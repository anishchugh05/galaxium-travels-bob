# Race Detective

Race Detective is a reusable Bob skill that finds, proves, and fixes concurrency bugs — lost updates, oversell races, double-cancels, check-then-act patterns — across any stack. It enforces a strict test-first rule: a race that cannot be demonstrated with a failing test is recorded as theoretical and left alone.

---

## Why it exists

The first two races in this project — the `book_flight` oversell and the `cancel_booking` seat-restore lost update — were found and fixed by hand. After doing that work twice, the pattern was clear enough to codify: scan for the signals, write a failing test, apply the minimal atomic fix, verify. Race Detective is that pattern as a repeatable Bob skill.

---

## Track record

| Codebase | Stack | What it found | Proven before → after |
|---|---|---|---|
| Galaxium Python backend (IBM's original code) | Python / FastAPI / SQLite | Rediscovered the `book_flight` oversell **and** the `cancel_booking` lost update independently, plus a **new bug we had missed**: double-cancel of the same booking restoring the seat twice | Before: 2 cancels succeeded, seats 0 → 2 (1 phantom seat). After: 1 succeeded, 1 rejected `ALREADY_CANCELLED`, seats 1. Fix: `UPDATE bookings SET status='cancelled' WHERE id=? AND status='booked'` (commit a5a64bb) |
| Galaxium Java hold service | Java / Spring Boot / JPA | 6 findings total; 4 fixed with 3 changes (hold and quote ID collisions shared one fix): double-confirm (Python booking endpoint called twice per hold), `count()+1` ID collisions under concurrency, and a confirm-vs-expire race in the expiry scheduler. Skipped 2 as low impact (double-release is harmless; duplicate holds per quote are covered by the confirm fix) | Double-confirm — before: Python client called 2×. After: 1×. Fix: atomic claim `HELD → CONFIRMING` before calling Python; revert to `HELD` on failure. ID collision — before: primary-key violation. After: UUID-based IDs, collision-free |

---

## How it works

Four phases, each gated on the previous:

| Phase | What happens |
|---|---|
| **1 — Scan** | Read-only grep for racy signals: ORM attribute mutation before commit, `SELECT` then `UPDATE` on the same row, status-check then state-write, shared counters. Produces a findings table with confidence levels. |
| **2 — Report** | Present the table; ask which findings to pursue. Do nothing until answered. |
| **3 — Prove** | For each chosen finding, write a failing concurrency test *before* touching production code. Must use a file-based DB (not in-memory/StaticPool) and fire all workers simultaneously. Show the "before" numbers. Only proceed if the test fails reliably. |
| **4 — Fix** | Apply the minimal atomic fix from `fix-recipes.md`. Rerun the concurrency test (must pass), then run the full suite (no regressions). Show a before/after summary. |

**Hard rules:**
- Never fix without a failing test first. No reliable failure = theoretical finding = stop.
- `SELECT FOR UPDATE` is silently ignored by SQLite. The only safe atomic pattern there is `UPDATE … WHERE <guard>` checked via `rowcount`.
- Do not swallow errors that leave state inconsistent — surface them as an `ErrorResponse`.
- One fix at a time, minimal diff.
- The test-first rule earned its keep: on the Java service the skill first claimed the ID race "silently overwrites" records — the failing test showed it actually throws a primary-key violation.

---

## Install in any repo

1. Copy `.bob/skills/race-detective/` into the target repo's `.bob/skills/` directory.
2. Reload the Bob window.
3. Confirm the skill appears under **Bob Settings → Skills**.

> **Note:** In our testing (Bob IDE 2.2.1) the global `~/.bob/skills/` location was not picked up; the project-level `.bob/skills/` copy was required.

---

## Example prompts

**Scan only — get the findings table without fixing anything:**
> "Run the race-detective skill on `services/booking.py` and `services/waitlist.py`, but stop after the Report phase and wait for my input before doing anything else."

**Targeted fix — skip straight to proving a specific finding:**
> "Use the race-detective skill to go straight to Phase 3 for the double-confirm race in `HoldService.java` — write a failing concurrency test, show me the before numbers, then fix it."

---

## Files in the skill

| File | Purpose |
|---|---|
| [`SKILL.md`](../.bob/skills/race-detective/SKILL.md) | Phase definitions, hard rules, and the before/after summary format Bob must produce |
| [`patterns.md`](../.bob/skills/race-detective/patterns.md) | Catalogue of six racy patterns (P1–P6) with examples in Python, Java, and TypeScript, plus grep recipes for Phase 1 scanning |
| [`fix-recipes.md`](../.bob/skills/race-detective/fix-recipes.md) | Four atomic fix recipes (conditional UPDATE, SELECT FOR UPDATE, optimistic locking, unique constraint) with a decision guide by database |
| [`test-template.md`](../.bob/skills/race-detective/test-template.md) | Concurrency test templates for Python/pytest, Java/JUnit 5, and Node/Jest, plus a table of common mistakes that hide the race |
