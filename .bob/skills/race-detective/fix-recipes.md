# Atomic Fix Recipes

Choose the recipe that matches your stack. Read the SQLite warning first — it applies to many
Python projects and is the most common mistake.

---

## ⚠️ SQLite warning — `SELECT FOR UPDATE` does nothing

SQLite parses `SELECT … FOR UPDATE` without error but **does not acquire any lock**. The clause
is silently ignored. The only safe atomic pattern on SQLite is the conditional `UPDATE` checked
via `rowcount` (Recipe 1). Do not use Recipe 2 with SQLite.

---

## Recipe 1 — Conditional UPDATE + rowcount check
**Works with:** SQLite, PostgreSQL, MySQL, SQL Server, any database
**Pattern type:** P1, P2, P3 — any counter decrement or status transition

Replace a read-check-write sequence with a single `UPDATE … WHERE <guard>`. The database
engine serialises concurrent updates to the same row; only one will match the guard.

### Python / SQLAlchemy (the Galaxium fix)
```python
from sqlalchemy import update

# Decrement — only if seats > 0
result = db.execute(
    update(Flight)
    .where(Flight.flight_id == flight_id, Flight.economy_seats_available > 0)
    .values(economy_seats_available=Flight.economy_seats_available - 1)
    .execution_options(synchronize_session=False)
)
if result.rowcount == 0:
    return ErrorResponse(error="No seats available", ...)

# Restore (cancel) — unconditional increment, always atomic
db.execute(
    update(Flight)
    .where(Flight.flight_id == flight_id)
    .values(economy_seats_available=Flight.economy_seats_available + 1)
    .execution_options(synchronize_session=False)
)
```

`execution_options(synchronize_session=False)` is required when updating a column you've already
loaded into the session — without it, SQLAlchemy tries to reconcile the Python object and raises
an error or silently returns stale data.

### Raw SQL
```sql
-- Decrement with guard
UPDATE flights
SET    economy_seats_available = economy_seats_available - 1
WHERE  flight_id = :id
  AND  economy_seats_available > 0;
-- check affected rows in your driver: cursor.rowcount / result.rowcount
```

### Node / Prisma
```ts
const result = await prisma.flight.updateMany({
  where: { id: flightId, economySeatsAvailable: { gt: 0 } },
  data:  { economySeatsAvailable: { decrement: 1 } },
});
if (result.count === 0) throw new NoSeatsError();
```

### Status transition (cancel / activate)
```python
# Only cancel if still booked — prevents double-cancel race
result = db.execute(
    update(Booking)
    .where(Booking.booking_id == booking_id, Booking.status == "booked")
    .values(status="cancelled")
    .execution_options(synchronize_session=False)
)
if result.rowcount == 0:
    return ErrorResponse(error="Booking already cancelled or not found", ...)
```

---

## Recipe 2 — SELECT FOR UPDATE (advisory row lock)
**Works with:** PostgreSQL, MySQL — **NOT SQLite**
**Pattern type:** P1, P2, P3, P4 — when you need to read the row before computing the new value

Acquires a row-level exclusive lock on `SELECT`, preventing any other transaction from reading
the same row with `FOR UPDATE` until your transaction commits or rolls back.

### PostgreSQL / psycopg2 / SQLAlchemy
```python
from sqlalchemy import select
from sqlalchemy.orm import Session

with db.begin():  # explicit transaction block
    flight = (
        db.execute(
            select(Flight)
            .where(Flight.flight_id == flight_id)
            .with_for_update()           # ← acquires row lock
        )
        .scalars()
        .one()
    )
    if flight.economy_seats_available < 1:
        raise NoSeatsError()
    flight.economy_seats_available -= 1  # safe: we hold the lock
    # lock released at transaction end
```

### Java / JPA
```java
Flight flight = em.find(Flight.class, id, LockModeType.PESSIMISTIC_WRITE);
if (flight.getSeats() < 1) throw new NoSeatsException();
flight.setSeats(flight.getSeats() - 1);
// lock released at @Transactional boundary
```

### Node / Prisma (raw query fallback)
```ts
// Prisma does not expose FOR UPDATE natively; use $queryRaw
await prisma.$transaction(async (tx) => {
  const [flight] = await tx.$queryRaw<Flight[]>`
    SELECT * FROM "Flight" WHERE id = ${flightId} FOR UPDATE
  `;
  if (flight.economySeatsAvailable < 1) throw new NoSeatsError();
  await tx.flight.update({
    where: { id: flightId },
    data:  { economySeatsAvailable: { decrement: 1 } },
  });
});
```

**Drawback:** under high concurrency, `FOR UPDATE` serialises all competing requests through a
bottleneck. Recipe 1 is usually faster and works on every database.

---

## Recipe 3 — Optimistic locking (version column / @Version)
**Works with:** PostgreSQL, MySQL, SQL Server, H2 — **NOT SQLite (no retry support in ORM)**
**Pattern type:** P1, P2 — when contention is low and you can tolerate retries

Add a `version` column. The UPDATE only succeeds if the version matches what you read. On
mismatch, the ORM raises `StaleObjectStateException` (JPA) or `OptimisticLockError` — retry.

### Java / Spring JPA
```java
@Entity
public class Flight {
    @Version
    private Long version;  // managed automatically by Hibernate
    // ...
}

// service — no explicit locking needed; Hibernate handles it
@Transactional
public void bookSeat(Long flightId) {
    Flight f = repo.findById(flightId).orElseThrow();
    if (f.getSeats() < 1) throw new NoSeatsException();
    f.setSeats(f.getSeats() - 1);
    // Hibernate emits: UPDATE flight SET seats=?, version=? WHERE id=? AND version=?
    // If another transaction committed first, version mismatch → OptimisticLockException
}

// caller retries on OptimisticLockException
```

### SQLAlchemy (manual version column)
```python
from sqlalchemy import Column, Integer
from sqlalchemy.orm import Session

class Flight(Base):
    __tablename__ = "flights"
    version = Column(Integer, default=0, nullable=False)
    # ...

# service
result = db.execute(
    update(Flight)
    .where(Flight.flight_id == flight_id, Flight.version == read_version)
    .values(seats=Flight.seats - 1, version=Flight.version + 1)
    .execution_options(synchronize_session=False)
)
if result.rowcount == 0:
    raise RetryableConflictError()
```

---

## Recipe 4 — Unique constraint / partial index
**Works with:** any database with constraint support
**Pattern type:** P3, P6 — prevent duplicate records (double-booking, double-spend)

When the race would result in a duplicate row rather than a wrong count, a unique constraint
is often the simplest fix. Let the database enforce uniqueness and catch the `IntegrityError`.

### SQLAlchemy model
```python
from sqlalchemy import UniqueConstraint

class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # One active booking per user per flight
        UniqueConstraint("user_id", "flight_id",
                         postgresql_where="status = 'booked'",
                         name="uq_one_active_booking"),
    )
```

### PostgreSQL partial index (migration)
```sql
CREATE UNIQUE INDEX uq_one_active_booking
    ON bookings (user_id, flight_id)
    WHERE status = 'booked';
```

Catch the error in the service layer:
```python
from sqlalchemy.exc import IntegrityError

try:
    db.commit()
except IntegrityError:
    db.rollback()
    return ErrorResponse(error="Duplicate booking", ...)
```

---

## Decision guide

| Database   | Best first choice                       | Avoid                        |
|------------|-----------------------------------------|------------------------------|
| SQLite     | Recipe 1 (conditional UPDATE + rowcount)| Recipe 2 (FOR UPDATE no-op)  |
| PostgreSQL | Recipe 1 or Recipe 2                    | —                            |
| MySQL      | Recipe 1 or Recipe 2                    | —                            |
| JPA/ORM    | Recipe 3 (@Version) or Recipe 1 (JPQL) | ORM object mutation alone    |
| Prisma     | Recipe 1 (updateMany with where)        | Recipe 2 (needs $queryRaw)   |
