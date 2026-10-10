---
name: race-detective
description: Use when the user wants to find, prove, and fix concurrency bugs — lost updates, oversell races, double-cancels, check-then-act patterns on shared counters, balances, inventory, or status fields. Trigger phrases: race condition, lost update, oversell, concurrency bug, atomic fix, check-then-act.
---

# Race Detective

A structured four-phase workflow for finding and fixing concurrency bugs.
Supporting files live alongside this one:
- [`patterns.md`](patterns.md) — catalogue of racy patterns with short examples
- [`fix-recipes.md`](fix-recipes.md) — atomic fix recipes per stack
- [`test-template.md`](test-template.md) — how to write a reproducing concurrency test

---

## Phase 1 — Scan (read-only)

1. Read the files the user points you at. If they did not specify files, grep for these signals:
   - ORM object attribute mutation followed by a commit (`obj.field +=`, `obj.field -=`, then `session.commit()`)
   - `SELECT` then `UPDATE` in separate statements on the same row
   - Status-check then state-write (`if obj.status == "active": obj.status = "done"`)
   - Counters, balances, inventory columns, or any "available/used/remaining" field
   - Shared mutable state accessed from multiple threads or async tasks
2. Do **not** touch any file during this phase.
3. Open `patterns.md` for the canonical racy-pattern catalogue.

**Output:** a findings table (see below). Then stop and ask which findings to pursue before doing anything else.

```
| # | File:line | Pattern type              | Confidence  | Notes                         |
|---|-----------|---------------------------|-------------|-------------------------------|
| 1 | foo.py:42 | read-modify-write counter | confirmed   | obj.seats -= 1 then commit    |
| 2 | bar.py:17 | check-then-act status     | likely      | if cancelled: raise; set done |
```

Confidence levels:
- **confirmed** — classic R-M-W or CTA with no compensating lock visible in the same code path
- **likely** — pattern present; a lock or serialisation mechanism may exist upstream
- **theoretical** — plausible race but only triggerable under unusual concurrency

---

## Phase 2 — Report

Present the table. Ask:
> "Which findings should I prove with a failing test? (Give me numbers, or 'all')"

Wait for the answer before proceeding.

---

## Phase 3 — Prove (test first, always)

For each chosen finding:

1. Open `test-template.md` and follow its structure exactly.
2. Write a failing test **before** changing any production code. Key requirements:
   - Use a **file-based DB** (not in-memory / StaticPool) so threads get genuinely independent connections.
   - Use a `ThreadPoolExecutor` (or equivalent) with a barrier or near-simultaneous start so the race window is actually hit.
   - Assert the exact expected count (e.g. `seats == CAPACITY`, `balance == INITIAL`). The test must fail on the buggy code.
3. Run the test and show the failure output (the "before" numbers).
4. Only then move to Phase 4.

> **Rule:** Never proceed to Phase 4 if the test does not fail on the current code. A race that cannot be demonstrated is theoretical — record it as such and stop.

---

## Phase 4 — Fix

1. Open `fix-recipes.md` and select the right recipe for this stack.
2. Apply the minimal atomic fix:
   - **SQLite / SQLAlchemy:** conditional `UPDATE … WHERE counter > 0` + `rowcount` check (SEE BELOW — `SELECT FOR UPDATE` does NOT work with SQLite)
   - **PostgreSQL / MySQL:** `SELECT … FOR UPDATE` inside a transaction, or conditional `UPDATE` + `rowcount`
   - **JPA / Hibernate:** `@Version` optimistic locking or a JPQL `UPDATE … WHERE` with `executeUpdate()`
   - **Node / Prisma:** atomic `update` with `where` condition + check on affected rows
3. Rerun the concurrency test — it must pass.
4. Run the full test suite — no regressions.
5. Show a before/after summary:

```
Before fix:
  Successful bookings : 8   (capacity was 5 — oversold by 3)
  Seats remaining     : -3

After fix:
  Successful bookings : 5   (exactly capacity)
  Seats remaining     : 0
```

---

## Hard rules

- **Never fix without a failing test first.** If the test cannot be made to fail reliably, mark the finding as theoretical and do not touch production code.
- **SQLite ignores `SELECT FOR UPDATE`** — it parses without error but provides no locking. The only safe atomic pattern on SQLite is `UPDATE … WHERE <condition>` checked via `rowcount`. See `fix-recipes.md`.
- **Do not swallow errors that leave state inconsistent.** If a conditional UPDATE matches 0 rows, surface that to the caller (e.g. `ErrorResponse`) — do not silently proceed and create a booking with no seat decremented.
- **One fix at a time.** Fix each finding independently so each gets its own failing test and passing test cycle.
- **Minimal change only.** Do not refactor unrelated code. The diff for each fix should be readable in under a minute.
