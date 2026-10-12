# Galaxium Travels × IBM Bob — Race Detective

**We used Bob to find, prove, and fix concurrency bugs in an unfamiliar 3-service app — then turned the method into a reusable Bob skill that found a bug we'd missed.**

---

[**Live demo**](https://galaxium-travels-bob-production.up.railway.app) · Track: **Theme 1 — Explore, Fix, and Build** · [Full story](docs/STORY.md)

---

## The headline

Five concurrency bugs found and fixed — two by hand with Bob, three by the race-detective skill.

| Bug | Before | After | Found by |
|---|---|---|---|
| `book_flight` oversell — lost update | 20 bookings on 5 seats succeeded; 3–4 seats still showed available (varies run to run) | 5 succeeded, 15 rejected, 0 remaining | Me + Bob (manual) |
| `cancel_booking` seat-restore — lost update | 10 concurrent cancels of different bookings restored only 2 seats | 10 cancels → exactly 10 seats restored | Me + Bob (manual) |
| `cancel_booking` double-cancel — same booking | 2 cancels both succeeded; seats 0 → 2 (1 phantom seat) | 1 succeeded, 1 rejected `ALREADY_CANCELLED`; seats = 1 | **race-detective skill** |
| Java double-confirm — hold service | One hold → Python booking endpoint called 2× → two bookings from one hold | Atomic `HELD → CONFIRMING` claim; Python called 1× | race-detective skill |
| Java ID collision — hold/quote PK | `count()+1` under concurrency → primary-key violation | UUID-based IDs; collision-free | race-detective skill |

<!-- TODO: demo GIF -->

---

## What we built

- **Concurrency fixes** — atomic conditional `UPDATE … WHERE <guard>` across Python (oversell, seat-restore, double-cancel) and Java (double-confirm, ID collision, expiry scheduler).
- **Waitlist feature** — join when a class is sold out; on cancel, the first waiter is promoted atomically in the same transaction. FIFO by PK, partial unique index, savepoint isolation.
- **`race-detective` Bob skill** — a 4-phase reusable skill (Scan → Report → Prove → Fix) with a hard test-first rule; re-ran on IBM's original code and found the double-cancel we had never looked for.

---

## How Bob was used

| Bob feature | What for | Evidence |
|---|---|---|
| Ask mode | Map the 3-service architecture; trace `book_flight()` end-to-end; flag the race condition | [Story §1–2](docs/STORY.md#1-explore) |
| Plan mode | Design the waitlist feature; reason through transaction boundaries | [Story §6](docs/STORY.md#6-build-on-solid-ground) |
| Agent mode | Implement fixes and the waitlist (Python + React); write failing concurrency tests | [Story §3–4](docs/STORY.md#3-prove) |
| Custom Skill | `race-detective` skill — codified the fix workflow; ran on Python and Java codebases | [Story §7–10](docs/STORY.md#7-teach-bob-the-lesson) · [RACE-DETECTIVE.md](docs/RACE-DETECTIVE.md) |
| Code review | Reviewed the waitlist implementation; caught a real `leave_waitlist` race + a savepoint gap | [Story §9](docs/STORY.md#9-second-opinion) |
| AI commit messages | Every fix committed with Bob-generated messages tracing the change to the bug | commit history |
| AGENTS.md context | IBM's existing AGENTS.md gave Bob the repo's footguns and conventions from the first prompt | [AGENTS.md](AGENTS.md) |

---

## Where I challenged Bob

Three strongest examples — full table in [DECISIONS.md § Where Bob was wrong](docs/DECISIONS.md#where-bob-was-wrong):

1. **`with_for_update()` for the waitlist claim** — SQLite silently ignores it, so the race would remain. Used an atomic conditional `UPDATE` + `rowcount` instead.
2. **`position = MAX + 1` for queue order** — itself racy: two concurrent joiners read the same `MAX` and collide. Used autoincrement PK directly.
3. **Oversell left seats at −15** — Bob's description; the actual test output showed 3–4 seats. Lost updates collapse writes, they don't subtract below zero.

---

## Improvements made during the hackathon

Everything below was changed from IBM's original [`galaxium-travels`](https://github.com/IBM/galaxium-travels):

- **`book_flight()` oversell fix** — atomic `UPDATE … WHERE economy_seats_available > 0` with rowcount check (commit `742b358`)
- **`cancel_booking()` seat-restore fix** — atomic `UPDATE flights SET seats = seats + 1` (no read-modify-write)
- **`cancel_booking()` unknown `seat_class` fix** — refuse with an error; never cancel without restoring the seat (commit `b28bcaa`)
- **Double-cancel fix** — `UPDATE bookings SET status='cancelled' WHERE id=? AND status='booked'` (commit `a5a64bb`)
- **Waitlist feature** — backend (commit `c699575`) + React frontend (commit `eb66f99`): join, FIFO promotion, partial unique index, savepoint isolation
- **Java double-confirm fix** — atomic `HELD → CONFIRMING` state claim before calling Python; revert on failure
- **Java UUID IDs** — replaced `count()+1` with UUID for hold and quote IDs
- **Java expiry scheduler** — made atomic via bulk conditional `UPDATE`
- **Java `Hold` equality** — `@EqualsAndHashCode(of = "holdId")` replacing Lombok `@Data`'s field-by-field comparison (pre-existing test failure on IBM's untouched code)
- **Postgres compatibility** — partial unique index now includes `postgresql_where` as well as `sqlite_where`
- **`race-detective` Bob skill** — `.bob/skills/race-detective/` (commit `737dd7f`): 4 files, reusable across any repo
- **Code-review fixes** — promotion isolated in a savepoint so a failure never blocks a cancel; `leave_waitlist` race fixed (commit `b81d069`)
- **Full test suite** — Python 97/97, Java 125/125, E2E 10/10 (all green)

---

## Try it

Alice books the last Galaxium seat. Bob joins the waitlist. Alice cancels. Bob is promoted automatically.

> Use two browser windows (one normal, one incognito) and register both users. If Flight #1 Galaxium is already sold out, use the Galaxium class on another flight.

1. Open the [live demo](https://galaxium-travels-bob-production.up.railway.app).
2. Sign in as **Alice** → search Earth → Mars → book the last **Galaxium** seat on Flight #1.
3. Sign in as **Bob** → search the same route → the Galaxium class shows sold out → click **Join waitlist**.
4. Switch back to Alice → go to My Bookings → cancel the booking.
5. Switch to Bob → refresh My Bookings → a new **Booking #22** (or similar) appears — Bob was promoted from the waitlist.

---

## Run it locally

```bash
# Start all three services
./start.sh
```

> **Java requirement:** the hold service requires **Java 17 or 21**. Java 22+ breaks Lombok and will not build.

**Run the tests:**

```bash
# Python backend (97/97)
cd booking_system_backend && .venv/bin/pytest

# Java hold service (125/125)
cd booking_system_inventory_hold_service && mvn test

# E2E suite — runs natively, no Docker needed (9 tests)
./test.sh
# Add the ~90 s hold-expiry test:
E2E_RUN_SLOW=1 ./test.sh  # 10/10
```

Final counts: **Python 97/97 · Java 125/125 · E2E 10/10**

---

## Docs

| Document | What's in it |
|---|---|
| [STORY.md](docs/STORY.md) | 13-chapter narrative: how each bug was found, proved, fixed, and what Bob got wrong |
| [DECISIONS.md](docs/DECISIONS.md) | Every key technical decision with reasoning, tradeoffs, and the full "Where Bob was wrong" table |
| [RACE-DETECTIVE.md](docs/RACE-DETECTIVE.md) | The reusable Bob skill — how it works, its track record, install instructions, and example prompts |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System overview, booking and cancel/promotion sequence diagrams, hold and waitlist state machines |
| [FACTS.md](docs/FACTS.md) | Single source of truth for all numbers, commits, and claims in this repo |
| [BASE-APP-README.md](docs/BASE-APP-README.md) | IBM's original README for the base `galaxium-travels` app, unchanged |

---

## Known limitations

Deliberate tradeoffs documented with proposed next fixes in [DECISIONS.md § Known limitations](docs/DECISIONS.md#known-limitations).

---

## Credits

Base app by **IBM** ([IBM/galaxium-travels](https://github.com/IBM/galaxium-travels)).  
Stack: React 19 + TypeScript + Vite · Python FastAPI + SQLite · Java 17 Spring Boot 3 · IBM Bob.
