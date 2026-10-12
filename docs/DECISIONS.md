# DECISIONS

This document records the key technical decisions made during development of the Galaxium Travels waitlist and concurrency features, and every place AI-generated output was challenged and corrected. It is derived entirely from [FACTS.md](FACTS.md) — no numbers or claims are invented here.

---

## Key decisions

### 1. Atomic conditional UPDATE over SELECT FOR UPDATE (SQLite)

**Context.** The original `book_flight()` used a read-then-write pattern: fetch the flight row, check seats, decrement. Under concurrency, 20 concurrent bookings on a 5-seat flight all passed the check before any decrement landed, letting all 20 succeed.

**Decision.** Replace the read-check-write with a single conditional UPDATE:

```python
update(Flight)
    .where(Flight.flight_id == flight_id, seat_col > 0)
    .values({seat_col.key: seat_col - 1})
```

Check `result.rowcount == 0` to detect the "no seats left" case ([`booking.py` line 77](../booking_system_backend/services/booking.py#L77)).

**Why.** SQLite serialises writers at the file level, so exactly one session wins the `WHERE seats > 0` guard per concurrent batch. The same pattern applies to cancel's seat restore and to double-cancel prevention (`WHERE status = 'booked'`).

**Tradeoff.** `SELECT FOR UPDATE` is idiomatic on Postgres, but SQLite silently ignores it — a footgun that would have left the bug in place on the target DB. The conditional UPDATE works identically on both.

---

### 2. FIFO by `waitlist_id`

**Context.** When a seat becomes available, the queue must produce a deterministic winner.

**Decision.** Claim the entry with the lowest `waitlist_id` using `SELECT MIN(waitlist_id) … WHERE status = 'waiting'`, then immediately flip that row to `'promoting'` with a targeted `UPDATE … WHERE waitlist_id = :id AND status = 'waiting'` ([`waitlist.py` line 214](../booking_system_backend/services/waitlist.py#L214)).

**Why.** `waitlist_id` is an autoincrement PK, so the smallest value is the earliest joiner. No separate position column is needed.

**Tradeoff.** An alternative of `position = MAX + 1` was proposed but is itself racy: two concurrent joiners can read the same MAX and collide. Using the PK avoids the extra write and the race.

---

### 3. Partial unique index (`status = 'waiting'` only)

**Context.** We want at most one active waitlist entry per `(user_id, flight_id, seat_class)`, but users must be able to rejoin after being cancelled or after a failed promotion.

**Decision.** A partial unique index covering only `status = 'waiting'` rows ([`models.py` lines 63–64](../booking_system_backend/models.py#L63)):

```python
Index(
    'uq_waitlist_active',
    'user_id', 'flight_id', 'seat_class',
    unique=True,
    sqlite_where=text("status = 'waiting'"),
    postgresql_where=text("status = 'waiting'"),
)
```

**Why.** A full unique index across all statuses would permanently lock a user out after their first booking or cancellation, because the historical rows would keep matching.

**Tradeoff.** Both `sqlite_where` and `postgresql_where` are required; SQLAlchemy is dialect-specific here. Without `postgresql_where`, Postgres would create a plain full unique index — one that covers *all* statuses — and would block users from rejoining after a previous booking or cancellation.

---

### 4. Promotion in the same transaction + savepoint

**Context.** When a booking is cancelled, the cancellation (status → `'cancelled'`, seats + 1) and the waitlist promotion (create booking, mark entry `'booked'`) must be atomic — either both land or neither does.

**Decision.** `attempt_promotion()` is called before `db.commit()` inside a `db.begin_nested()` savepoint ([`booking.py` line 177](../booking_system_backend/services/booking.py#L177)):

```python
with db.begin_nested():  # SAVEPOINT
    attempt_promotion(db, booking.flight_id, booking.seat_class)
```

The single `db.commit()` at the end of `cancel_booking()` owns the entire transaction.

**Why.** Without the savepoint, a promotion crash would also roll back the cancel itself — the seat counter would not be restored and the booking would remain `'booked'`. With the savepoint, a promotion failure rolls back only the promotion; the cancel still commits.

**Tradeoff.** The promotion path must not call `db.commit()` internally. The earlier prototype called `book_flight()`, which commits internally, breaking the single-transaction contract. `_create_booking()` (no commit) replaced it.

---

### 5. Skipping failed promotions

**Context.** A promotion can fail for two distinct reasons: the seat was grabbed by a concurrent direct booker (`NO_SEATS_AVAILABLE`), or something unexpected went wrong (user deleted, DB error, etc.).

**Decision.** The two cases are handled differently ([`waitlist.py` lines 14–20](../booking_system_backend/services/waitlist.py#L14)):

- **`NO_SEATS_AVAILABLE`** (seat grabbed by a racing direct booker): reset the entry to `'waiting'` and stop — there is no seat to give anyone, so continuing would be pointless.
- **Any other exception**: mark the entry `'failed'` and loop to try the next `'waiting'` entry.

**Why.** Resetting a `NO_SEATS_AVAILABLE` case to `'waiting'` is correct — the user did nothing wrong and should keep their place when a seat next opens. Resetting an unexpected-error entry to `'waiting'` would re-queue a poisoned entry at the front of the line, jamming the queue indefinitely. Marking it `'failed'` and continuing means later waiters are still served.

**Tradeoff.** The seat returns to general inventory if no subsequent waiter can be promoted, favouring availability over strict fairness. This is documented in [Known limitations](#known-limitations).

---

### 6. HELD → CONFIRMING claim in Java

**Context.** The Java hold service called the Python booking endpoint inside `confirmHold()`. Under concurrent confirm requests, Python was called twice for the same hold, creating two bookings.

**Decision.** Before calling Python, atomically transition the hold: `HELD → CONFIRMING` with a single bulk-UPDATE ([`HoldRepository.java` line 20](../booking_system_inventory_hold_service/src/main/java/com/galaxium/holdservice/repository/HoldRepository.java#L20)):

```java
UPDATE Hold h SET h.status = CONFIRMING …
WHERE h.holdId = :holdId AND h.status = HELD …
```

If `claimForConfirm()` returns `rowcount == 0`, another caller already owns it; return early. On Python failure, revert `CONFIRMING → HELD` so the caller can retry.

**Why.** Only one caller can win the `WHERE status = HELD` guard. The `CONFIRMING` state is transient — its sole purpose is to serialise confirm attempts.

**Tradeoff.** A hold stuck in `CONFIRMING` (Java crashes mid-confirm) cannot self-heal without manual intervention. See [Known limitations](#known-limitations).

---

### 7. UUID IDs for holds and quotes

**Context.** The original Java service generated hold and quote IDs using `count() + 1`. Under concurrency, two concurrent creates read the same count and generate the same ID, producing a primary-key violation.

**Decision.** Generate IDs with UUID ([`HoldService.java` line 173](../booking_system_inventory_hold_service/src/main/java/com/galaxium/holdservice/service/HoldService.java#L173)):

```java
return "H-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12).toUpperCase();
```

**Why.** UUID generation requires no DB round-trip and is collision-free under any level of concurrency.

**Tradeoff.** IDs are less human-readable than sequential integers, but that is acceptable for a booking reference.

---

### 8. PK-based equality for `Hold`

**Context.** Lombok `@Data` on the `Hold` entity generated `equals()`/`hashCode()` that compared every field. Two `Hold` objects created at different instants (even for the same conceptual hold) would never be equal, and any `Instant` field comparison would fail by nanoseconds.

**Decision.** Override with `@EqualsAndHashCode(of = "holdId")` ([`Hold.java` line 19](../booking_system_inventory_hold_service/src/main/java/com/galaxium/holdservice/domain/Hold.java#L19)):

```java
@EqualsAndHashCode(of = "holdId")
```

**Why.** JPA best practice: entity identity is the primary key. Field-by-field equality is semantically wrong for mutable entities and breaks any collection that relies on stable hash codes across `save()`/`refresh()` cycles.

**Tradeoff.** A `Hold` with a `null` ID (not yet persisted) is equal to any other `null`-ID hold. In practice, holds are always persisted before comparison, but it is a known edge case.

---

## Where Bob was wrong

| What Bob proposed or claimed | What was actually true | How we found out |
|---|---|---|
| Use `SELECT FOR UPDATE` to guard the seat decrement | SQLite silently ignores `SELECT FOR UPDATE` — the race would remain | Reasoning about SQLite's locking model |
| Use `position = MAX + 1` for queue ordering | Two concurrent joins read the same `MAX` and collide — itself racy | Reasoning about the concurrent join path |
| Apply the unique constraint across all statuses | Users would be permanently locked out after their first booking or cancel | Reasoning about re-join semantics |
| Promotion should call `book_flight()` | `book_flight()` commits internally, breaking the single-transaction claim | Code inspection |
| Reset every failed promotion entry back to `'waiting'`, regardless of error | Re-queues a poisoned entry at the front — jams the queue forever | Reasoning about the promotion loop |
| The oversell left seats at −15 (went negative) | Seats actually stayed at 4 — lost updates, not underflow (20 decrements collapsed into 1) | Test output |
| The first cancel fix on bad `seat_class` silently skipped the seat restore but still cancelled the booking | That is an inventory leak — correct behaviour is to refuse with an error and never cancel without restoring the seat | Verification + code review |
| Skill draft contained fabricated example numbers ("8 bookings, seats −3") | A lost-update race cannot produce a negative seat count — lost updates collapse writes, they don't subtract below zero | User challenge during skill authoring |
| Code review flagged 3 "criticals": claimed a failed promotion could leave a booking cancelled with its seat unrestored | `flush()` ≠ committed — the whole transaction rolls back together; the only real issue was UX (promotion failure blocked the cancel), fixed with a savepoint | Reasoning: SQLAlchemy transaction semantics |
| The ID race "silently overwrites" the existing hold/quote | The failing test showed it actually throws a primary-key violation | Test output |
| `@EqualsAndHashCode(of="holdId")` provides null-ID safety | It does not — two not-yet-persisted holds with `holdId = null` are equal | Reasoning about Lombok's generated code |
| Without `postgresql_where`, Postgres would allow duplicate waiting rows for the same user (too loose) | Without it, Postgres creates a *full* unique index covering all statuses — too strict: users can never rejoin after leaving or being promoted | Reasoning about SQLAlchemy DDL dialect behaviour |

---

## Known limitations

From FACTS.md — each with the fix we would do next.

| Limitation | Next fix |
|---|---|
| **Java → Python timeout:** if Python creates the booking but the HTTP response times out, a retry by the caller could create a duplicate booking | Make the `/internal/bookings/from-hold` endpoint idempotent, keyed on `hold_id` — return the existing booking if one already exists for that hold |
| **Stuck CONFIRMING:** if the Java service crashes between claiming `CONFIRMING` and completing the confirm, the hold stays in `CONFIRMING` indefinitely | Add a recovery path in the expiry scheduler: treat holds stuck in `CONFIRMING` for longer than the hold duration as expired |
| **Promotion failure drops seat to general inventory:** if no waiter can be promoted (all fail), the seat is returned to direct booking rather than held for the queue | Add a retry window or notify the next waiter explicitly before releasing to general inventory |
| **Proxy returns HTTP 200 with error body on Java 404s** (pre-existing) | Return the actual HTTP status from the Java service; callers should not need to inspect the body to detect an error |
| **SQLite data resets on each redeploy; waitlist promotions are logged but not notified** | Persist state across deploys (Postgres / persistent volume); add a notification mechanism (email or in-app) when a waitlisted user is promoted |
