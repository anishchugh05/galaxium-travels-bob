# Racy Pattern Catalogue

Reference for Phase 1 scanning. Each entry shows the dangerous idiom and why it races.

---

## P1 — Read-Modify-Write on a shared counter (lost update)

The classic lost update. Two sessions each read the same value, each compute the new value
independently, and each write — one write is silently discarded.

### Python / SQLAlchemy (ORM mutation)
```python
# RACY — two sessions both read seats=1, both decrement to 0, both commit
flight = db.query(Flight).filter_by(flight_id=id).first()
if flight.economy_seats_available < 1:
    raise NoSeatsError()
flight.economy_seats_available -= 1   # ← ORM tracks the Python object, not the DB row
db.commit()
```

### Java / JPA
```java
// RACY — same problem via managed entity mutation
Flight flight = em.find(Flight.class, id);
if (flight.getSeats() < 1) throw new NoSeatsException();
flight.setSeats(flight.getSeats() - 1);  // ← mutation of in-memory object
// transaction commit — another thread may have done the same thing
```

### Node / Prisma
```ts
// RACY
const flight = await prisma.flight.findUnique({ where: { id } });
if (flight.seats < 1) throw new Error('no seats');
await prisma.flight.update({ where: { id }, data: { seats: flight.seats - 1 } });
// another request read the same seats value in between
```

**Scan signal:** `obj.field += / -=` or `obj.setField(obj.getField() ± 1)` followed by a commit,
with no `SELECT FOR UPDATE`, `@Version`, or conditional `WHERE` clause guarding it.

---

## P2 — Check-then-act on status or availability

A guard check (`if seats > 0`, `if status == 'active'`) followed by a state-changing write,
with no lock holding the guard valid across both operations.

```python
# RACY
seats = db.query(Flight.economy_seats_available).filter_by(...).scalar()
if seats < 1:          # ← check happens here, in Python
    return error
# ... time passes, another thread also passed this check ...
flight.economy_seats_available -= 1   # ← act happens here
db.commit()
```

Also appears as status transitions:
```python
if booking.status == "cancelled":
    return AlreadyCancelledError()
# gap — another request could also have passed the check
booking.status = "cancelled"
db.commit()
```

**Scan signal:** `if obj.field == <value>:` or `if count < 1:` followed by a separate write
to the same field, without a holding lock between them.

---

## P3 — Double-cancel / double-spend

A specific instance of P2. Two concurrent requests both pass the `status != cancelled` check,
both proceed to cancel (or both debit a balance), and both commit. Inventory/balance ends up
inconsistent.

```python
# Thread A and Thread B both reach here with booking.status == "booked"
booking = db.query(Booking).get(booking_id)
if booking.status == "cancelled":   # both pass
    return error
booking.status = "cancelled"        # both write
flight.seats += 1                   # both increment — seats over-restored
db.commit()
```

**Scan signal:** any cancel/refund/spend path that reads-then-writes status without a conditional
UPDATE or row-level lock.

---

## P4 — Aggregate-read then write

Read an aggregate (`COUNT`, `SUM`, scalar subquery) and then take action based on that aggregate.
The aggregate is stale by the time the write happens.

```python
# RACY
active_holds = db.query(func.count()).filter_by(flight_id=id, status='active').scalar()
if active_holds >= capacity:
    return CapacityError()
hold = Hold(flight_id=id, ...)
db.add(hold)
db.commit()
```

---

## P5 — ORM lazy-load inside a concurrent path

In async frameworks (FastAPI with `async def`, Starlette), accessing a lazily-loaded relationship
after the session has been yielded to another coroutine can hit a detached-instance error or
silently return stale data. Not a classic race but can cause data loss.

```python
# Potentially racy in async context
booking = await db.get(Booking, id)
# session may have been used by another coroutine between these two lines
flight = booking.flight   # ← lazy load fires here against a potentially-reused connection
```

---

## P6 — Non-atomic multi-table update

Updating two tables in separate statements without wrapping them in a single transaction.
A crash or concurrent write between the two statements leaves the database in a half-updated state.

```java
// RACY — not atomic
flightRepo.decrementSeat(flightId);   // statement 1
bookingRepo.save(newBooking);          // statement 2 — a crash between these loses the decrement
```

**Scan signal:** two `save()` / `execute()` / `update()` calls against different tables with no
enclosing `@Transactional` or explicit `BEGIN` / `COMMIT`.

---

## Quick scan grep patterns

```bash
# Python — ORM mutation (look for increment/decrement of a mapped column)
grep -n '\.\(economy\|business\|seats\|balance\|count\|inventory\) [+-]=' **/*.py

# Python — check then act on status
grep -n 'status ==' **/*.py | head -30

# Java — entity field mutation outside a transaction
grep -rn '\.set[A-Z].*get[A-Z]' src/ | grep -v '@Transactional'

# Node/TS — non-atomic read then update
grep -n 'findUnique\|findFirst' **/*.ts | head -20
# cross-reference with nearby .update() calls
```
