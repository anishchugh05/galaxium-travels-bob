# Concurrency Test Template

A concurrency test that does not actually reproduce the race is worse than no test — it gives
false confidence. Follow these rules exactly.

---

## The three requirements

1. **Separate connections.** Each concurrent worker must open its own database connection
   (or session/entity-manager). A shared in-memory database with a single connection serialises
   all writes and will never show a race.

2. **Simultaneous start.** Workers must reach the contested code path at the same time.
   Use a `Barrier` or fire all futures before collecting results, not sequentially.

3. **Assert exact counts.** Check the final DB state with a fresh query after all workers finish.
   Assert the exact number you expect, not just "no exception was raised."

---

## Python / pytest template (SQLite file-based)

Based directly on [`test_concurrency.py`](../../booking_system_backend/tests/test_concurrency.py)
from the Galaxium races.

```python
"""
Concurrency test for <describe what you are testing>.

Uses a file-based SQLite DB (not in-memory / StaticPool) so each thread gets a
genuinely independent connection — the same isolation failure as production.
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from myapp.models import Base, Widget
from myapp.services import widget_service

CAPACITY = 5           # seats / slots / budget units available
WORKERS  = 20          # concurrent requests — must be > CAPACITY to stress the race


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def file_db(tmp_path_factory):
    """File-based engine; each thread creates its own Session from it."""
    db_path = tmp_path_factory.mktemp("race") / "race_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()
    if db_path.exists():
        db_path.unlink()


@pytest.fixture()
def seeded(file_db):
    """Seed the shared resource, yield it, then wipe rows for the next test."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=file_db)
    session = SessionLocal()

    widget = Widget(capacity=CAPACITY, used=0)
    session.add(widget)
    session.commit()
    widget_id = widget.id
    session.close()

    yield file_db, widget_id

    # teardown — wipe so fixtures are reusable
    cleanup = SessionLocal()
    cleanup.query(Widget).delete()
    cleanup.commit()
    cleanup.close()


# ── worker ────────────────────────────────────────────────────────────────────

def _worker(engine, widget_id: int):
    """Open a fresh session per worker — do NOT share sessions across threads."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        return widget_service.claim_slot(db, widget_id=widget_id)
    finally:
        db.close()


# ── test ──────────────────────────────────────────────────────────────────────

class TestClaimSlotRace:
    def test_concurrent_claims_do_not_exceed_capacity(self, seeded):
        """
        WORKERS concurrent claims against a resource with CAPACITY slots.
        Expected to FAIL before the atomic fix; must PASS after.
        """
        engine, widget_id = seeded
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        # Fire all futures before collecting — this is the barrier
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(_worker, engine, widget_id) for _ in range(WORKERS)]
            results = [f.result() for f in as_completed(futures)]

        successes = [r for r in results if r is not None and not r.is_error]
        errors    = [r for r in results if r is None or r.is_error]

        # Re-read with a fresh session to get the committed DB state
        final = SessionLocal()
        widget = final.query(Widget).filter_by(id=widget_id).first()
        slots_used = widget.used
        final.close()

        print(f"\n  Successes   : {len(successes)}  (capacity = {CAPACITY})")
        print(f"  Errors      : {len(errors)}")
        print(f"  Slots used  : {slots_used}")
        print(f"  Oversold by : {max(0, len(successes) - CAPACITY)}")

        assert len(successes) == CAPACITY, (
            f"Expected {CAPACITY} successes, got {len(successes)} "
            f"— oversold by {len(successes) - CAPACITY}"
        )
        assert slots_used == CAPACITY, (
            f"Expected used={CAPACITY}, got {slots_used}"
        )
```

---

## Java / JUnit 5 template (H2 in-memory, separate EntityManagers)

```java
@SpringBootTest
@Transactional(propagation = Propagation.NOT_SUPPORTED)  // disable outer TX so each thread's TX is independent
class ClaimSlotConcurrencyTest {

    @Autowired WidgetService service;
    @Autowired WidgetRepository repo;

    static final int CAPACITY = 5;
    static final int WORKERS  = 20;

    @BeforeEach
    void seed() {
        repo.deleteAll();
        repo.save(new Widget(CAPACITY, 0));
    }

    @Test
    void concurrentClaimsShouldNotExceedCapacity() throws Exception {
        Long widgetId = repo.findAll().get(0).getId();

        ExecutorService pool = Executors.newFixedThreadPool(WORKERS);
        CountDownLatch startLatch = new CountDownLatch(1);  // barrier

        List<Future<Boolean>> futures = new ArrayList<>();
        for (int i = 0; i < WORKERS; i++) {
            futures.add(pool.submit(() -> {
                startLatch.await();   // all threads wait here
                return service.claimSlot(widgetId);
            }));
        }

        startLatch.countDown();   // release all threads simultaneously
        pool.shutdown();
        pool.awaitTermination(10, TimeUnit.SECONDS);

        long successes = futures.stream().filter(f -> {
            try { return f.get(); } catch (Exception e) { return false; }
        }).count();

        Widget widget = repo.findById(widgetId).orElseThrow();

        System.out.printf("Successes: %d (capacity %d)%n", successes, CAPACITY);
        System.out.printf("Slots used: %d%n", widget.getUsed());

        assertEquals(CAPACITY, successes,
            "Expected " + CAPACITY + " successes, got " + successes);
        assertEquals(CAPACITY, widget.getUsed());
    }
}
```

---

## Node / Jest template (separate PrismaClient instances)

```ts
// Each worker must use its own PrismaClient (own connection pool)
import { PrismaClient } from '@prisma/client';

const CAPACITY = 5;
const WORKERS  = 20;

async function worker(widgetId: number): Promise<boolean> {
  const prisma = new PrismaClient();   // own connection — critical
  try {
    return await widgetService.claimSlot(prisma, widgetId);
  } finally {
    await prisma.$disconnect();
  }
}

test('concurrent claims do not exceed capacity', async () => {
  const widget = await seedWidget(CAPACITY);

  // fire all promises before awaiting — simultaneous start
  const results = await Promise.allSettled(
    Array.from({ length: WORKERS }, () => worker(widget.id))
  );

  const successes = results.filter(r => r.status === 'fulfilled' && r.value).length;

  const final = await prisma.widget.findUnique({ where: { id: widget.id } });
  console.log(`Successes: ${successes}, used: ${final?.used}`);

  expect(successes).toBe(CAPACITY);
  expect(final?.used).toBe(CAPACITY);
});
```

---

## Common mistakes that hide the race

| Mistake | Why it hides the race |
|---|---|
| In-memory SQLite / StaticPool shared across threads | Single connection serialises all writes |
| Sequential `for` loop calling the service | No concurrency; race window never opens |
| `await`ing each promise before firing the next | Same — effectively serial |
| Asserting "no exception" instead of exact counts | A race that corrupts data often raises no exception |
| Too few workers (< 2× capacity) | Race window hit too rarely to fail consistently |
| Sleeping instead of using a barrier / simultaneous fire | Threads drift and miss the race window |

---

## Confirming the test actually detects the bug

Before applying any fix, revert (or temporarily comment out) the atomic guard and run the test.
If it does not fail reliably within 3 runs, the test is not detecting the race — debug the
concurrency setup before claiming the race exists.

A good concurrency test fails **consistently** on the buggy code and passes **consistently** on
the fixed code. If it is flaky in either direction, something is wrong with the test setup.
