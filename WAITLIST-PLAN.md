# Per-Flight Waitlist — Implementation Plan

## Top-Level Overview

Add a per-flight, per-seat-class waitlist to the booking system.  
When a seat class is full, a logged-in user can join the waitlist. When a booking is
cancelled, the first waiting user for that flight+class is promoted atomically in the
same transaction as the seat restore — so no direct booker can snatch the seat in
between. The frontend shows a "Join Waitlist" button where a class is sold out and
surfaces waitlist status on the My Bookings page.

**Scope:**
- Python backend only (FastAPI + SQLAlchemy + SQLite).
- React/TypeScript frontend (Flights page + My Bookings page).
- Backend unit tests (services) + concurrency tests (file-based SQLite).
- No changes to the Java hold service, e2e suite, or Docker/deploy config.

**Non-goals:**
- Email/push notification on promotion (log only).
- Admin queue management.
- Waitlist expiry / TTL.
- Waitlist for the hold (quote→hold) path.

---

## Architecture Summary

```
POST /waitlist          → join_waitlist() service
GET  /waitlist/{user_id} → get_waitlist() service
DELETE /waitlist/{entry_id} → leave_waitlist() service

cancel_booking() (existing)
  └─ seat restore UPDATE        (existing, atomic)
  └─ attempt_promotion()        (NEW — no internal commit)
       └─ loop:
            atomic UPDATE waitlist SET status='promoting'
            WHERE waitlist_id = (SELECT MIN(waitlist_id) FROM waitlist
                                 WHERE status='waiting' AND flight_id=X AND seat_class=Y)
            → rowcount=0: no waiters, exit loop
            → rowcount=1: re-fetch promoting entry, look up user.name
                 call _create_booking() — no commit, appends to session
                 on BookingOut:  SET status='booked', exit loop
                 on NO_SEATS_AVAILABLE: SET status='waiting', exit loop (race: seat gone)
                 on other error: SET status='failed', continue loop (try next waiter)
  └─ caller cancel_booking() sets booking.status='cancelled'
  └─ single db.commit() covers all writes atomically
```

Key refactors:
- `book_flight()` currently calls `db.commit()` internally (line ~88). Extract a
  non-committing `_create_booking()` used by both `book_flight()` (which adds its own
  commit after) and `attempt_promotion()` (which defers to the caller's commit).
- Claim UPDATE uses `WHERE waitlist_id = (SELECT MIN(...))` subquery, NOT
  `ORDER BY / LIMIT` — SQLite's default build does not support UPDATE...ORDER BY.
- Unique constraint is a PARTIAL index `WHERE status='waiting'`, not a full table
  constraint, so users can rejoin after leaving or being promoted.
- On promotion failure: only `NO_SEATS_AVAILABLE` resets to `'waiting'`; any other
  error marks `'failed'` and the loop tries the next waiter.

---

## Sub-Tasks

---

### Sub-Task 1 — Waitlist Model & Schema

**Status:** `[x] done`

**Intent**
Define the `Waitlist` ORM model and the Pydantic schemas needed to expose it via REST.
Everything downstream (service, endpoint, frontend type) depends on this shape.

**Expected Outcomes**
- `Waitlist` table created in SQLite on startup via `Base.metadata.create_all()`.
- `WaitlistOut` Pydantic schema serialisable from ORM with `from_attributes=True`.
- `WaitlistRequest` Pydantic schema validates join requests.
- Partial unique index (`WHERE status='waiting'`) prevents duplicate active entries but
  allows the same user to rejoin after leaving or being promoted.
- No existing tests broken.

**Todo List**
1. Add `Waitlist` class to `models.py` after the `Booking` class:
   - `waitlist_id`: Integer, primary key, autoincrement — serves as queue order.
   - `user_id`: Integer, ForeignKey `users.user_id`, NOT NULL.
   - `flight_id`: Integer, ForeignKey `flights.flight_id`, NOT NULL.
   - `seat_class`: String, NOT NULL.
   - `status`: String, NOT NULL, default `'waiting'` — values: `waiting | promoting | booked | cancelled | failed`.
   - `added_at`: String, NOT NULL — ISO datetime string (matches `booking_time` pattern in `Booking`).
   - Add a SQLAlchemy `Index` with `sqlite_where` for the partial unique constraint:
     `Index('uq_waitlist_active', 'user_id', 'flight_id', 'seat_class', unique=True, sqlite_where=text("status='waiting'"))`.
     This prevents two simultaneous `'waiting'` entries for the same user/flight/class
     but allows a user to rejoin after their entry reaches `'booked'`, `'cancelled'`, or `'failed'`.
2. Add `WaitlistOut` to `schemas.py`:
   - Fields: `waitlist_id`, `user_id`, `flight_id`, `seat_class`, `status`, `added_at`.
   - `model_config = ConfigDict(from_attributes=True)` (matches `BookingOut` pattern).
3. Add `WaitlistRequest` to `schemas.py`:
   - Fields: `user_id: int`, `name: str`, `flight_id: int`, `seat_class: SeatClass`.
   - `name` is required because `book_flight()` re-validates `user_id AND name` at promotion time.

**Relevant Context**
- `models.py` — add after line 40 (end of `Booking` class); `text` must be imported from `sqlalchemy`.
- `schemas.py` — `BookingOut` (lines 58-67) and `SeatClass = Literal[...]` (line 6) are the patterns to follow.
- `SeatClass` is already defined as `Literal['economy', 'business', 'galaxium']` — reuse it.

---

### Sub-Task 2 — Waitlist Service + `_create_booking` Refactor

**Status:** `[x] done`

**Intent**
Refactor `book_flight()` to extract a non-committing `_create_booking()` inner helper,
then implement the waitlist service functions and the promotion loop.

**Expected Outcomes**
- `_create_booking(db, user_id, flight_id, seat_class, price_paid)` appends a new
  `Booking` to the session but does NOT commit — callers own the commit.
- `book_flight()` behaviour is externally unchanged: it still validates, decrements the
  seat, calls `_create_booking()`, commits, and returns `BookingOut`.
- `join_waitlist()` rejects if seats still available, user not found/name mismatch, or
  duplicate active entry.
- `get_waitlist_for_user()` returns all non-cancelled, non-failed entries.
- `leave_waitlist()` errors (409) if entry is not in `'waiting'` status.
- `attempt_promotion()` loops until it either promotes a waiter, finds no waiters, or
  exhausts the queue:
  - Claims entry with `WHERE waitlist_id = (SELECT MIN(waitlist_id) ... WHERE status='waiting' ...)`.
  - On `NO_SEATS_AVAILABLE`: marks entry `'waiting'` (seat gone in race), exits — no point trying next waiter.
  - On any other `ErrorResponse`: marks entry `'failed'`, loops to try next waiter.
  - On `BookingOut`: marks entry `'booked'`, exits.
- All writes inside one `db.commit()` owned by `cancel_booking()`.

**Todo List**
1. In `booking.py`, extract `_create_booking(db, user_id, flight_id, seat_class, price_paid)`:
   - Creates the `Booking` ORM object, calls `db.add()`, returns the unsaved object.
   - Does NOT call `db.commit()` or `db.refresh()`.
2. Update `book_flight()` to call `_create_booking()` then `db.commit()` then `db.refresh()` (same external behaviour, no test changes needed).
3. Create `booking_system_backend/services/waitlist.py`.
4. Implement `join_waitlist(db, user_id, name, flight_id, seat_class) -> WaitlistOut | ErrorResponse`:
   - Validate seat_class is a known class.
   - Check flight exists.
   - Check user exists and name matches (`USER_NOT_FOUND` / `NAME_MISMATCH`).
   - Check seat class is actually full; if seats > 0 return `SEATS_AVAILABLE`.
   - Check no existing `'waiting'` entry for this user/flight/class; if found return `ALREADY_ON_WAITLIST`.
   - Insert `Waitlist(status='waiting', added_at=utc_now_iso)`, commit, refresh, return `WaitlistOut`.
5. Implement `get_waitlist_for_user(db, user_id) -> list[WaitlistOut]`:
   - Return entries where `status NOT IN ('cancelled', 'failed')`.
6. Implement `leave_waitlist(db, waitlist_id, user_id) -> WaitlistOut | ErrorResponse`:
   - 404 if entry not found (`WAITLIST_ENTRY_NOT_FOUND`).
   - 409 if `entry.user_id != user_id` (`NOT_YOUR_ENTRY`).
   - 409 if `status != 'waiting'` (`CANNOT_LEAVE` — entry is already promoted/cancelled).
   - Set `status='cancelled'`, commit, return `WaitlistOut`.
7. Implement `attempt_promotion(db, flight_id, seat_class) -> None` — internal, no commit:
   - Loop indefinitely:
     a. Atomic claim: `UPDATE waitlist SET status='promoting' WHERE waitlist_id = (SELECT MIN(waitlist_id) FROM waitlist WHERE status='waiting' AND flight_id=:fid AND seat_class=:sc)`.
     b. If `rowcount == 0`: break (no waiters left).
     c. Re-fetch the `'promoting'` entry; look up `user.name` from `users` table.
     d. Call `_create_booking()` after verifying seat still available with the same atomic `UPDATE flights … WHERE seats > 0` check (reuse `book_flight`'s seat-decrement pattern directly).
        - Because `_create_booking()` does not validate or decrement seats itself, `attempt_promotion()` must do the seat decrement step directly before calling `_create_booking()`.
        - Alternative (simpler): call `book_flight()` which does everything; since `book_flight()` now commits internally, `attempt_promotion()` must call `_create_booking()` + the seat decrement UPDATE directly.
        - Chosen approach: replicate the seat-decrement atomic UPDATE inside `attempt_promotion()`, then call `_create_booking()`. This avoids importing `book_flight` into `waitlist.py` (circular import risk) and keeps the no-commit contract.
     e. Seat decrement result:
        - `rowcount == 0` (NO_SEATS_AVAILABLE race): mark entry `'waiting'`, break.
        - `rowcount == 1`: call `_create_booking()`, mark entry `'booked'`, break.
     f. If an unexpected exception occurs: mark entry `'failed'`, continue loop.
8. Modify `cancel_booking()` in `booking.py`:
   - Import `attempt_promotion` from `services.waitlist`.
   - Call `attempt_promotion(db, booking.flight_id, booking.seat_class)` after the seat-restore UPDATE and before `booking.status = 'cancelled'`.
   - No change to `db.commit()` — it already covers all writes.

**Relevant Context**
- `booking.py` lines 80-92 — `_create_booking()` extraction point: `db.add(new_booking)` through `db.commit()` / `db.refresh()`.
- `booking.py` lines 63-68 — seat-decrement atomic UPDATE pattern to replicate inside `attempt_promotion()`.
- `booking.py` lines 128-141 — insertion point for `attempt_promotion()` call.
- Circular import note: `waitlist.py` imports from `models` and `schemas` only; it does NOT import from `booking.py`. `booking.py` imports `attempt_promotion` from `waitlist.py`. No cycle.

---

### Sub-Task 3 — REST Endpoints

**Status:** `[x] done`

**Intent**  
Expose the waitlist service functions as REST endpoints and map `ErrorResponse` objects
to the correct HTTP status codes, following the existing pattern in `server.py`.

**Expected Outcomes**
- `POST /waitlist` → joins the waitlist; returns `WaitlistOut` (201) or 404/409 on error.
- `GET /waitlist/{user_id}` → returns `list[WaitlistOut]` (200).
- `DELETE /waitlist/{waitlist_id}` → leaves the waitlist; returns `WaitlistOut` (200) or 404/409.
- All three endpoints automatically become MCP tools via the existing `FastApiMCP(app)` at line 333 of `server.py` — no extra MCP wiring needed.

**Todo List**
1. Add `from services import waitlist as waitlist_service` import in `server.py`.
2. Add `from schemas import WaitlistOut, WaitlistRequest` import in `server.py`.
3. Implement `POST /waitlist` endpoint:
   - Request body: `WaitlistRequest`.
   - Delegate to `waitlist_service.join_waitlist(db, ...)`.
   - Error mapping: `FLIGHT_NOT_FOUND` / `USER_NOT_FOUND` → 404; all others → 409.
   - Success: return `WaitlistOut` with `status_code=201`.
4. Implement `GET /waitlist/{user_id}` endpoint:
   - Delegate to `waitlist_service.get_waitlist_for_user(db, user_id)`.
   - Return `list[WaitlistOut]`.
5. Implement `DELETE /waitlist/{waitlist_id}` endpoint:
   - Accept `user_id` as a query parameter (ownership check needs it).
   - Delegate to `waitlist_service.leave_waitlist(db, waitlist_id, user_id)`.
   - Error mapping: `WAITLIST_ENTRY_NOT_FOUND` → 404; others → 409.
6. Group all three endpoints under `tags=["Waitlist"]` (matches the existing tag pattern).

**Relevant Context**
- `server.py` lines 152-186 — the `book_flight_endpoint` and `cancel_booking_endpoint` are the exact patterns to follow.
- `server.py` line 333: `mcp = FastApiMCP(app)` — placed after all route definitions; new endpoints added before this line are auto-exposed as MCP tools.
- HTTP status code mapping convention: `404` for not-found errors, `409` for conflict/constraint errors.

---

### Sub-Task 4 — Backend Unit Tests

**Status:** `[x] done`

**Intent**
Cover all service-layer behaviours with unit tests using the existing `db_session`
fixture (in-memory SQLite + StaticPool). Concurrency tests for promotion go in
`test_concurrency.py` (file-based SQLite).

**Expected Outcomes**
- `test_waitlist_service.py` covers all happy paths and error paths for `join_waitlist`, `get_waitlist_for_user`, `leave_waitlist`.
- `test_waitlist_service.py` `TestPromotionUnit` covers promotion happy path, FIFO order, no-waiter cancel, and the `'failed'` skip-to-next-waiter behaviour.
- `test_concurrency.py` `TestWaitlistPromotionRace` fires 10 concurrent cancels against a full flight with 10 waitlisted users and asserts exactly 10 promotions, 0 double-promotions, 0 seats leaked.
- Full suite passes with 0 failures.

**Todo List**
1. Create `booking_system_backend/tests/test_waitlist_service.py`.
2. `TestWaitlistService`:
   - `test_join_waitlist_success`
   - `test_join_waitlist_seats_available` → `SEATS_AVAILABLE`
   - `test_join_waitlist_already_on_list` → `ALREADY_ON_WAITLIST`
   - `test_join_waitlist_user_not_found` → `USER_NOT_FOUND`
   - `test_join_waitlist_name_mismatch` → `NAME_MISMATCH`
   - `test_join_waitlist_flight_not_found` → `FLIGHT_NOT_FOUND`
   - `test_join_waitlist_rejoin_after_cancel`: cancel an entry, then same user can join again (validates partial unique index).
   - `test_get_waitlist_empty` → `[]`
   - `test_get_waitlist_excludes_cancelled`: cancelled entries not returned.
   - `test_leave_waitlist_success` → entry `status='cancelled'`
   - `test_leave_waitlist_not_found` → `WAITLIST_ENTRY_NOT_FOUND`
   - `test_leave_waitlist_wrong_user` → `NOT_YOUR_ENTRY`
   - `test_leave_waitlist_already_promoted` → `CANNOT_LEAVE` (entry is `'booked'`)
3. `TestPromotionUnit` (uses `db_session`):
   - `test_cancel_promotes_waitlisted_user`: cancel booking → entry `'booked'`, new `Booking` row, `seats_available == 0`.
   - `test_cancel_no_waitlist`: cancel → seat+1, no new booking row.
   - `test_cancel_promotes_fifo`: two waiters → lower `waitlist_id` promoted first.
   - `test_cancel_skips_failed_entry`: seed one entry that will fail (e.g. corrupt `seat_class` on waiter's context — simulate by temporarily forcing the seat decrement to return rowcount=0 for first waiter), confirm second waiter is promoted.
     Simpler approach: seed entry with a user_id that does not exist in `users` table — `_create_booking` will not fail on that (it just inserts), so instead directly test `attempt_promotion()` with a mocked scenario, or accept that the failed-entry path is covered by the concurrency test.
   - Revised: `test_cancel_skips_failed_entry` — directly call `attempt_promotion()` after manually inserting a `'promoting'`-status entry (simulating a crashed mid-promotion), confirm it skips it and promotes the next `'waiting'` entry.
4. `TestWaitlistPromotionRace` in `test_concurrency.py`:
   - `file_db`-scoped engine (same as existing tests).
   - Seed: 1 flight (0 seats), 10 bookings (10 bookers), 10 waitlist entries (10 waiters), all different users.
   - Fire: 10 threads each calling `cancel_booking(engine, booking_id)`.
   - Assert:
     - Total `Booking` rows with `status='booked'` == 20 (10 original + 10 promoted).
     - Zero `Waitlist` entries with `status='waiting'` or `'promoting'`.
     - `economy_seats_available == 0` (seat freed and immediately re-claimed each time).

**Relevant Context**
- `tests/conftest.py` — `db_session` creates schema fresh per test via `Base.metadata.create_all()`; `Waitlist` table will appear automatically once the model is in `models.py`.
- `tests/test_services.py` lines 360-445 — seed pattern for `User`, `Flight`, `Booking`.
- `tests/test_concurrency.py` `TestCancelRace` — exact model for `TestWaitlistPromotionRace` fixture structure.

---

### Sub-Task 5 — Frontend: Join Waitlist on Flights Page

**Status:** `[x] done`

**Intent**  
On the Flights page, when a seat class is sold out, show a "Join Waitlist" button
instead of (or alongside) "Sold Out". Clicking it joins the waitlist for that
flight+class via the new backend endpoint.

**Expected Outcomes**
- `FlightCard` shows a "Join Waitlist" button per sold-out seat class (not just a global one for the whole flight).
- The button is only shown when the user is logged in (no anonymous waitlist).
- On success, the button changes to "On Waitlist ✓" and is disabled.
- On error (already on list, etc.), a toast error is shown.
- No layout changes to classes that still have seats available.

**Todo List**
1. Add `WaitlistEntry` TypeScript type to `types/index.ts`:
   - `waitlist_id: number`, `user_id: number`, `flight_id: number`, `seat_class: SeatClass`, `status: 'waiting' | 'booked' | 'cancelled'`, `added_at: string`.
2. Add `joinWaitlist(data: WaitlistRequest)` and `getUserWaitlist(userId: number)` to `api.ts`:
   - `joinWaitlist`: `POST /waitlist`, returns `WaitlistEntry | ErrorResponse`.
   - `getUserWaitlist`: `GET /waitlist/{userId}`, returns `WaitlistEntry[]`.
   - Follow the existing `bookFlight` / `cancelBooking` pattern.
3. Modify `FlightCard.tsx`:
   - Accept an optional `waitlistEntries?: WaitlistEntry[]` prop and `onJoinWaitlist?: (flightId: number, seatClass: SeatClass) => void` callback prop.
   - Per seat-class row: if `seats === 0` and user is logged in, render a "Join Waitlist" button instead of "Sold Out" text; if already on the waitlist for this class, render "On Waitlist ✓" (disabled).
   - Keep the existing "Sold Out" display for logged-out users.
4. Modify `Flights.tsx` (the flights listing page):
   - Fetch the user's current waitlist entries on load alongside flights (call `getUserWaitlist`).
   - Implement `handleJoinWaitlist(flightId, seatClass)` that calls `joinWaitlist`, shows a toast, and refreshes the waitlist state.
   - Pass `waitlistEntries` and `onJoinWaitlist` down to each `FlightCard`.

**Relevant Context**
- `FlightCard.tsx` lines 14-165 — seat class rows are already per-class; the sold-out display at line 161 (`'Sold Out'`) is the insertion point.
- `Flights.tsx` lines 143-171 — `FlightCard` is rendered here; props are passed from the page.
- `api.ts` lines 122-147 — `bookFlight` and `cancelBooking` are the exact patterns for `joinWaitlist`.
- `types/index.ts` line 37-42 — `BookingRequest` is the model for `WaitlistRequest`.
- User context is already available on the Flights page (check how it is used on `MyBookings.tsx` line 64).

---

### Sub-Task 6 — Frontend: Waitlist Status on My Bookings Page

**Status:** `[x] done`

**Intent**  
Show the user's active waitlist entries on the My Bookings page so they can see where
they are in the queue and leave if they no longer want the seat.

**Expected Outcomes**
- A "Waitlist" section appears on My Bookings (below or alongside active bookings).
- Each entry shows: flight route, seat class, queue status ("Waiting for a seat").
- A "Leave Waitlist" button removes the entry.
- When a waitlist entry has `status='booked'` it no longer appears in the waitlist section (it appears as a normal active booking instead).

**Todo List**
1. Add `leaveWaitlist(waitlistId: number, userId: number)` to `api.ts`:
   - `DELETE /waitlist/{waitlistId}?user_id={userId}`, returns `WaitlistEntry | ErrorResponse`.
2. Modify `MyBookings.tsx`:
   - Add `waitlistEntries` state (`WaitlistEntry[]`).
   - Load waitlist alongside bookings in `loadData()` using `getUserWaitlist(user.user_id)`.
   - Filter to only show `status === 'waiting'` entries in the waitlist section.
   - Render a new `WaitlistCard` component (or inline section) for each waiting entry.
   - Implement `handleLeaveWaitlist(waitlistId)` that calls `leaveWaitlist`, shows a toast, and refreshes.
3. Create `booking_system_frontend/src/components/bookings/WaitlistCard.tsx`:
   - Props: `entry: WaitlistEntry`, `flight?: Flight`, `onLeave: (waitlistId: number) => void`, `isLeaving?: boolean`.
   - Display: flight route + seat class + "Waiting for a seat" status badge.
   - "Leave Waitlist" button (danger variant, matches `BookingCard` cancel button style).
   - Follow `BookingCard.tsx` structure (status icon, flight info layout).

**Relevant Context**
- `MyBookings.tsx` lines 64-82 — `loadData` pattern for parallel fetching.
- `MyBookings.tsx` lines 130-131 — `activeBookings` / `pastBookings` filter pattern; add a parallel `waitingEntries` filter.
- `BookingCard.tsx` lines 7-167 — the exact component structure and styling to mirror in `WaitlistCard`.
- `api.ts` lines 140-147 — `cancelBooking` is the model for `leaveWaitlist`.

---

## Cross-Cutting Decisions

| Constraint | Decision |
|---|---|
| SQLite ignores SELECT FOR UPDATE | Atomic claim via `UPDATE waitlist SET status='promoting' WHERE waitlist_id = (SELECT MIN(waitlist_id) FROM waitlist WHERE status='waiting' AND ...)` + rowcount check |
| No ORDER BY / LIMIT in UPDATE | SQLite default build does not support UPDATE...ORDER BY; use MIN subquery instead |
| Queue order | `waitlist_id` autoincrement — lower ID = joined earlier = promoted first (no position column) |
| Seat transfer atomicity | `_create_booking()` + seat-decrement UPDATE inside `attempt_promotion()`, all committed by the single `db.commit()` in `cancel_booking()` |
| `book_flight()` internal commit | Extracted `_create_booking()` does no commit; `book_flight()` adds its own commit after — external callers unaffected |
| Name at promotion time | `attempt_promotion()` re-queries `user.name` from DB; `WaitlistRequest` requires `name` only to validate at join time |
| Unique constraint | Partial index `WHERE status='waiting'` — allows rejoin after `'booked'`, `'cancelled'`, or `'failed'` |
| MCP tools | Automatic — `FastApiMCP(app)` at server.py line 333 picks up all new routes |
| Status values | `waiting → promoting → booked` (success) or `waiting → promoting → waiting` (seat-race only) or `waiting → promoting → failed` (other errors, queue advances) or `waiting → cancelled` (user leaves) |
| Promotion error handling | `NO_SEATS_AVAILABLE`: reset to `'waiting'`, stop loop. Any other error: mark `'failed'`, advance loop to next waiter. |
