# Story: Finding and Fixing Concurrency Bugs with Bob

---

## 1. Explore

I was handed IBM's `galaxium-travels` — a 3-service app I had never seen before: a React frontend, a Python FastAPI backend backed by SQLite, and a Java Spring Boot hold service. I opened it in Bob Ask mode and asked it to map the architecture, trace the data flow between services, and flag any obvious risks. Within a few prompts I had a clear picture of how a booking moves from the frontend through the Python API and, for holds, on to the Java service and back.

![Bob Ask mode mapping the 3-service architecture](images/01-explore.png) <!-- TODO screenshot: Ask mode session showing architecture overview or the request-flow diagram -->

---

## 2. Find

Still in Ask mode, I asked Bob to trace what happens when two users book the last seat at the same moment. It followed `book_flight()` to the seat decrement: the code fetched the flight row in Python, checked `economy_seats_available`, decremented the in-memory object, and committed — a classic read-modify-write with nothing serialising the gap between the check and the write.

---

## 3. Prove

Before touching any production code I had Bob write a failing concurrency test in Agent mode: 20 threads, each booking one economy seat on a flight with 5 available. On the unfixed code, all 20 succeeded and 4 seats still showed as "available" — 20 decrements collapsed into 1 because every thread read the same starting value before any commit landed. Bob initially reported this as seats going negative (−15); the test output showed it was actually 4, because lost updates suppress writes rather than accumulate them.

![Failing concurrency test output — 20 succeeded, 4 remaining](images/03-prove.png) <!-- TODO screenshot: pytest output showing 20 successes and seats_remaining=4 -->

---

## 4. Fix

The fix was a single conditional UPDATE in Agent mode: `UPDATE flights … WHERE seats > 0`, checked via `rowcount`. After the fix: 5 succeeded, 15 rejected, 0 seats remaining (commit 742b358). Bob's race report offered two options — a conditional UPDATE or `SELECT FOR UPDATE`; I chose the conditional UPDATE because SQLite silently ignores `SELECT FOR UPDATE`. The reasoning is recorded in [DECISIONS.md — Decision 1](DECISIONS.md#1-atomic-conditional-update-over-select-for-update-sqlite).

![After fix — 5 succeeded, 15 rejected, 0 seats remaining](images/04-fix.png) <!-- TODO screenshot: pytest output showing 5 successes and seats_remaining=0 -->

---

## 5. Same bug, different place

`cancel_booking()` had the mirror-image race: 10 concurrent cancels of different bookings, each reading the seat counter and doing `seats += 1` in Python. The fix was the same pattern in reverse — `UPDATE flights SET seats = seats + 1` with no guard needed, because an unconditional increment is always atomic. Test: 10 concurrent cancels → exactly 10 seats restored. Bob's first attempt at the cancel fix also introduced a bug I caught: on an unknown `seat_class` it silently skipped the seat restore but still cancelled the booking — an inventory leak. Fix: refuse with an error, never cancel without restoring (commit b28bcaa).

---

## 6. Build on solid ground

With the backend's concurrency correct, I designed the waitlist feature in Bob Plan mode: join when a class is sold out; on cancel, the first waiter gets the seat in the same transaction. Bob's initial plan had several problems I had to push back on — `position = MAX + 1` for ordering (itself racy), a full unique constraint across all statuses (would block users from rejoining), a promotion path that used `with_for_update()` (silently ignored by SQLite), a promotion path that called `book_flight()` internally (which commits, breaking the single-transaction guarantee), and resetting every failed promotion to `'waiting'` regardless of error type (would jam the queue). The full list of challenges and the decisions we landed on are in [DECISIONS.md — Decisions 2–5](DECISIONS.md#2-fifo-by-waitlist_id). Tests caught a real promotion bug during implementation: the promotion re-fetched the wrong row. The finished feature shipped as commits c699575 (backend) and eb66f99 (frontend), with a working Alice/Bob demo — Alice books the last seat, Bob joins the waitlist, Alice cancels, Bob is automatically promoted.

| 1. Alice books the last Galaxium seat | 2. Bob joins the waitlist |
|---|---|
| ![Alice booked](images/06a-alice-booked.png) | ![Bob waiting](images/06b-bob-waiting.png) |
| **3. Alice cancels** | **4. Bob is promoted automatically (new Booking #22)** |
| ![Alice cancelled](images/06c-alice-cancelled.png) | ![Bob promoted](images/06d-bob-promoted.png) |

---

## 7. Teach Bob the lesson

After fixing the same class of bug twice — the oversell race and the seat-restore race — the workflow was clear enough to codify (the `seat_class` leak was a logic bug, not a race). I turned it into the `race-detective` Bob skill (commit 737dd7f): four phases — Scan, Report, Prove with a failing test, Fix — with one hard rule: never fix without a failing test first. Bob's draft of the skill contained fabricated example numbers; I replaced them with the real numbers from our tests. The skill is documented in [RACE-DETECTIVE.md](RACE-DETECTIVE.md).

---

## 8. The skill finds what I missed

I ran the race-detective skill against IBM's original, unmodified code. It rediscovered the `book_flight` oversell and the `cancel_booking` seat-restore race on its own — and then found a bug I had never thought to look for: two concurrent cancels of the *same* booking both passed the `status != 'cancelled'` check, both succeeded, and seats went from 0 to 2, creating a phantom seat. Before: 2 cancels succeeded, seats 0 → 2. After: 1 succeeded, 1 rejected `ALREADY_CANCELLED`, seats 1. Fix: `UPDATE bookings SET status='cancelled' WHERE id=? AND status='booked'` (commit a5a64bb).

![Skill finding the double-cancel bug — before and after](images/08-double-cancel.png) <!-- TODO screenshot: test output or skill report showing the double-cancel finding and before/after seat counts -->

---

## 9. Second opinion

I ran Bob's code review over the waitlist implementation. It flagged 3 "criticals". Two were real: a failed promotion blocked the entire cancel (fixed by wrapping `attempt_promotion()` in a `db.begin_nested()` savepoint so a promotion failure rolls back only itself while the cancel still commits), and a `leave_waitlist` race where 2 concurrent leaves both succeeded (fixed so exactly 1 succeeds). One critical was wrong: Bob claimed that `flush()` calls could leave a booking cancelled with its seat unrestored, but `flush()` ≠ committed — the whole transaction rolls back together if anything raises. Knowing which findings to trust required understanding SQLAlchemy transaction semantics, not just reading the code review output (commit b81d069).

---

## 10. New language, same skill

I ran the race-detective skill on the Java hold service. It found 6 issues; 4 were fixed with 3 changes (hold and quote ID collisions shared one fix), and 2 were skipped as low impact. The most serious: concurrent confirm requests called the Python booking endpoint twice for the same hold, creating duplicate bookings. Fix: atomically claim `HELD → CONFIRMING` before calling Python; revert to `HELD` on failure — only one caller can win the `WHERE status = HELD` guard (see [DECISIONS.md — Decision 6](DECISIONS.md#6-held--confirming-claim-in-java)). The second: hold and quote IDs were generated with `count() + 1`, which collides under concurrency and throws a primary-key violation (Bob initially said "silently overwrites" — the failing test showed it actually throws). Fix: UUID-based IDs for both (Decision 7). The third: the expiry scheduler was made atomic with a bulk conditional UPDATE.

![Java hold service — double-confirm proven and fixed](images/10-java-hold.png) <!-- TODO screenshot: test output proving the double-confirm race, or the HELD→CONFIRMING state diagram -->

---

## 11. A bug that was there all along

While running the Java test suite I hit a failure in `HoldTest.testEqualsAndHashCode` — on IBM's untouched original code, before any of my changes (verified by stashing). Lombok's `@Data` had generated `equals()`/`hashCode()` that compared every field, including two `Instant.now()` calls that differ by nanoseconds. The test was asserting a contract that was never true. Fix: `@EqualsAndHashCode(of = "holdId")` — JPA best practice, entity identity is the primary key (see [DECISIONS.md — Decision 8](DECISIONS.md#8-pk-based-equality-for-hold)).

---

## 12. Ship it

I deployed all three services to Railway. Final counts: Python 97/97, Java 125/125, E2E 10/10 (including the slow hold-expiry test). The live app is at [galaxium-travels-bob-production.up.railway.app](https://galaxium-travels-bob-production.up.railway.app).

---

## 13. What I learned

- **The test is the proof.** Bob could describe a race plausibly but get the numbers wrong (−15, "silently overwrites"). The concurrency test is what tells you what actually happens — and it's the only thing that proves a fix worked.
- **Push back on the first suggestion.** `SELECT FOR UPDATE`, `MAX + 1`, full unique constraints, `book_flight()` in a promotion — each was the idiomatic first answer and each was wrong for this specific codebase. Reading the reasoning in [DECISIONS.md](DECISIONS.md) before accepting a proposal pays off.
- **A workflow you can repeat is worth more than a one-off fix.** Fixing two races by hand was useful; turning the method into a skill that then found a third bug I had missed was better.
