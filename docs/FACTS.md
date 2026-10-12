# FACTS — single source of truth for all docs (do not invent numbers beyond this file)

## Project
- Track: Theme 1 — Explore, Fix, and Build
- Base app: IBM/galaxium-travels (React frontend, Python FastAPI + SQLite backend, Java Spring Boot hold service)
- Repo: https://github.com/anishchugh05/galaxium-travels-bob
- Live demo: https://galaxium-travels-bob-production.up.railway.app (Railway, 3 services, SQLite)
- Final test counts: Python 102/102, Java 125/125, E2E 10/10 (incl. slow hold-expiry test)

## Fixes and features (in order)
1. book_flight oversell (lost update). Test: 20 concurrent bookings on 5 economy seats.
   Before: 20 succeeded, 0 rejected, seats remaining 4 (20 decrements collapsed into 1 — NOT negative).
   After: 5 succeeded, 15 rejected, seats remaining 0.
   Fix: atomic conditional UPDATE ... WHERE seats > 0 + rowcount check. (commit 742b358)
2. cancel_booking seat-restore lost update (concurrent cancels of different bookings). Fix: atomic UPDATE seats = seats + 1. Test: 10 concurrent cancels → exactly 10 seats restored.
3. cancel_booking with unknown seat_class silently skipped the seat restore but still cancelled. Fix: refuse with error, never cancel without restoring. (b28bcaa)
4. Waitlist feature (backend c699575, frontend eb66f99): join when a class is sold out; on cancel, first waiter gets the seat in the same transaction. FIFO by waitlist_id. Atomic claim via conditional UPDATE. Partial unique index (one active entry per user/flight/class). Failed promotions are skipped so the queue can't jam. Tests caught a promotion bug that re-fetched the wrong row.
5. race-detective Bob skill (.bob/skills/race-detective): Scan → Report → Prove with failing test → Fix. Rule: never fix without a failing test. (737dd7f)
6. Ran the skill on IBM's original code: it rediscovered both races on its own AND found a new bug we had missed — double-cancel of the SAME booking.
   Before: 2 cancels succeeded, seats 0 → 2 (1 phantom seat). After: 1 succeeded, 1 rejected ALREADY_CANCELLED, seats 1. Fix: UPDATE bookings SET status='cancelled' WHERE id=? AND status='booked'. (a5a64bb)
7. Bob code review fixes (b81d069): promotion wrapped in a savepoint so a failed promotion no longer blocks the cancel; leave_waitlist race fixed (2 concurrent leaves → exactly 1 succeeds); a cancel test that passed for the wrong reason was corrected.
8. Postgres compatibility: partial index now has postgresql_where as well as sqlite_where.
9. Java hold service — sskill found 6 issues; 4 fixed with 3 changes, 2 skipped:
   - Double-confirm: one hold → Python booking created twice. Before: Python client called 2×. After: 1×. Fix: atomic claim HELD → CONFIRMING before calling Python; revert to HELD on failure.
   - Hold/quote IDs from count()+1 collided under concurrency (primary-key violation). Fix: UUID-based IDs.
   - Expiry scheduler made atomic (bulk conditional UPDATE), closing the confirm-vs-expire race.
   - Skipped: double-release (harmless) and duplicate holds per quote (covered by the confirm fix).
10. Pre-existing bug: HoldTest.testEqualsAndHashCode failed on IBM's untouched code (verified by stashing our changes). Cause: Lombok @Data compared every field, and the test's two Instant.now() calls differ by nanoseconds. Fix: equality by holdId only (JPA best practice); updated one test that asserted the old, wrong contract.

## Where Bob was wrong and I corrected it
- Proposed SELECT FOR UPDATE — SQLite silently ignores it.
- Proposed position = MAX+1 for queue order — itself racy.
- Unique constraint on all statuses would lock users out of rejoining forever.
- Promotion called book_flight(), which commits internally — broke the single-transaction claim.
- Resetting failed promotions to 'waiting' would jam the queue.
- Reported seats "went negative / −15" — actually 4 (lost updates).
- First cancel fix "failed silently" on bad seat_class — a silent inventory leak.
- Its skill draft contained fake example numbers that would teach wrong reporting.
- Code review flagged 3 "criticals"; 2 were wrong (flushed ≠ committed — everything rolls back together).
- Claimed the ID race "silently overwrites" — the failing test showed it actually throws.
- Claimed @EqualsAndHashCode(of="holdId") gives null-id safety — it doesn't.
- Explained the Postgres index fix backwards (full index is too strict, not too loose).

## Known limitations (deliberate)
- Java→Python timeout: if Python books but the response times out, a retry could double-book. Fix: idempotent from-hold endpoint keyed on hold_id.
- A hold can stick in CONFIRMING if the Java service crashes mid-confirm.
- If promotion fails, the seat returns to general inventory (availability over strict fairness).
- Proxy returns HTTP 200 with an error body on Java 404s (pre-existing).
- SQLite data resets on each redeploy; waitlist promotion is logged, not notified.

## Bob features used
Ask mode (exploration), Plan mode (waitlist design), Agent mode (implementation), custom Skill (race-detective), Code review, AI commit messages, AGENTS.md context.

## Improvements Made 
- Waitlist polish — queue position ("You're #N in line") and a "You got a seat!" toast on promotion