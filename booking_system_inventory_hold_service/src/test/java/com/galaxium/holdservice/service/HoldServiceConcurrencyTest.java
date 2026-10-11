package com.galaxium.holdservice.service;

import com.galaxium.holdservice.client.PythonBackendClient;
import com.galaxium.holdservice.domain.Hold;
import com.galaxium.holdservice.domain.Quote;
import com.galaxium.holdservice.repository.AuditEventRepository;
import com.galaxium.holdservice.repository.HoldRepository;
import com.galaxium.holdservice.repository.QuoteRepository;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.test.context.ActiveProfiles;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Concurrency tests for HoldService.
 *
 * Uses a full Spring context with H2 (test profile) so each thread gets a
 * genuinely independent JPA transaction — the same isolation failure as production.
 *
 * These tests are EXPECTED TO FAIL on the unfixed code and MUST PASS after the fix.
 */
@SpringBootTest
@ActiveProfiles("test")
@DisplayName("HoldService - Concurrency Tests")
class HoldServiceConcurrencyTest {

    @Autowired
    private HoldService holdService;

    @Autowired
    private HoldRepository holdRepository;

    @Autowired
    private QuoteRepository quoteRepository;

    @Autowired
    private AuditEventRepository auditEventRepository;

    /** Mocked so we control call counts without a real Python backend. */
    @MockBean
    private PythonBackendClient pythonBackendClient;

    private static final String QUOTE_ID = "Q-RACE-001";
    private static final String HOLD_ID  = "H-RACE-001";

    // -------------------------------------------------------------------------
    // Test fixtures
    // -------------------------------------------------------------------------

    @BeforeEach
    void seedHoldAndQuote() {
        // Seed a valid, non-expired quote
        Quote quote = Quote.builder()
                .quoteId(QUOTE_ID)
                .flightId(1)
                .seatClass("economy")
                .quantity(1)
                .travelerId(42)
                .travelerName("Race Tester")
                .pricePerSeat(500_000L)
                .totalPrice(500_000L)
                .expiresAt(Instant.now().plus(24, ChronoUnit.HOURS))
                .status(Quote.QuoteStatus.CREATED)
                .build();
        quoteRepository.save(quote);

        // Seed a HELD hold in the valid window
        Instant now = Instant.now();
        Hold hold = Hold.builder()
                .holdId(HOLD_ID)
                .quoteId(QUOTE_ID)
                .status(Hold.HoldStatus.HELD)
                .reservedUntil(now.plus(15, ChronoUnit.MINUTES))
                .createdAt(now)
                .updatedAt(now)
                .build();
        holdRepository.save(hold);

        // Python client returns a successful booking by default
        PythonBackendClient.BookingResponse fakeBooking = new PythonBackendClient.BookingResponse();
        fakeBooking.setBookingId(99);
        when(pythonBackendClient.createBookingFromHold(anyMap())).thenReturn(fakeBooking);
    }

    @AfterEach
    void cleanup() {
        auditEventRepository.deleteAll();
        holdRepository.deleteAll();
        quoteRepository.deleteAll();
    }

    // =========================================================================
    // Finding #1 — double-confirm race
    // =========================================================================

    /**
     * Two concurrent confirmHold() calls on the same HELD hold.
     *
     * Expected (after fix):
     *   - Python backend is called EXACTLY ONCE — only one caller wins the HELD→CONFIRMING claim.
     *   - The hold ends up CONFIRMED.
     *   - The second caller receives an IllegalStateException (hold no longer in HELD status).
     *
     * Before the fix (check-then-act):
     *   - Both callers see status=HELD, both pass the guard, both call the Python backend.
     *   - pythonBackendClient.createBookingFromHold() is invoked TWICE → double-booking.
     */
    @Test
    @DisplayName("Two concurrent confirms on same hold must call Python backend exactly once")
    void testConcurrentConfirmCallsPythonBackendExactlyOnce() throws InterruptedException {
        int workers = 2;
        CountDownLatch startGun = new CountDownLatch(1);
        AtomicInteger successes = new AtomicInteger(0);
        AtomicInteger failures  = new AtomicInteger(0);

        ExecutorService pool = Executors.newFixedThreadPool(workers);
        List<Future<?>> futures = new ArrayList<>();

        for (int i = 0; i < workers; i++) {
            futures.add(pool.submit(() -> {
                try {
                    startGun.await();   // synchronised start
                    holdService.confirmHold(HOLD_ID);
                    successes.incrementAndGet();
                } catch (Exception e) {
                    failures.incrementAndGet();
                }
            }));
        }

        startGun.countDown();   // release both threads simultaneously
        pool.shutdown();
        for (Future<?> f : futures) {
            try { f.get(); } catch (Exception ignored) {}
        }

        // Re-read from DB with a fresh context
        Hold finalHold = holdRepository.findById(HOLD_ID).orElseThrow();

        System.out.printf("%n  Successful confirms : %d%n", successes.get());
        System.out.printf("  Failed confirms     : %d%n", failures.get());
        System.out.printf("  Final hold status   : %s%n", finalHold.getStatus());

        // The critical assertion: Python client must be called EXACTLY ONCE.
        verify(pythonBackendClient, times(1)).createBookingFromHold(anyMap());

        assertThat(successes.get()).isEqualTo(1);
        assertThat(failures.get()).isEqualTo(1);
        assertThat(finalHold.getStatus()).isEqualTo(Hold.HoldStatus.CONFIRMED);
    }

    // =========================================================================
    // Finding #3/#4 — concurrent createHold collisions (ID generation race)
    // =========================================================================

    /**
     * Multiple concurrent createHold() calls on the same quote.
     *
     * Expected (after fix):
     *   - All WORKERS creates succeed.
     *   - All generated holdIds are unique (no two callers compute the same ID).
     *   - The DB contains exactly WORKERS hold rows.
     *
     * Before the fix (count()+1 pattern):
     *   - Two threads read the same count, compute identical IDs.
     *   - JPA merge() with the same PK silently overwrites one row,
     *     OR the DB unique constraint fires → exception.
     */
    @Test
    @DisplayName("Concurrent createHold calls must produce unique hold IDs")
    void testConcurrentCreateHoldProducesUniqueIds() throws InterruptedException {
        // Use a different quoteId per call so we don't hit the hold-per-quote constraint
        int workers = 10;
        // Seed 10 extra quotes
        for (int i = 2; i <= workers; i++) {
            String qId = "Q-RACE-00" + i;
            Quote q = Quote.builder()
                    .quoteId(qId)
                    .flightId(i)
                    .seatClass("economy")
                    .quantity(1)
                    .travelerId(42 + i)
                    .travelerName("Racer " + i)
                    .pricePerSeat(100_000L)
                    .totalPrice(100_000L)
                    .expiresAt(Instant.now().plus(24, ChronoUnit.HOURS))
                    .status(Quote.QuoteStatus.CREATED)
                    .build();
            quoteRepository.save(q);
        }

        CountDownLatch startGun = new CountDownLatch(1);
        List<String> createdIds = new ArrayList<>();
        List<Exception> errors  = new ArrayList<>();
        Object lock = new Object();

        ExecutorService pool = Executors.newFixedThreadPool(workers);
        List<Future<?>> futures = new ArrayList<>();

        for (int i = 1; i <= workers; i++) {
            final String qId = "Q-RACE-00" + i;
            futures.add(pool.submit(() -> {
                try {
                    startGun.await();
                    Hold h = holdService.createHold(qId);
                    synchronized (lock) { createdIds.add(h.getHoldId()); }
                } catch (Exception e) {
                    synchronized (lock) { errors.add(e); }
                }
            }));
        }

        startGun.countDown();
        pool.shutdown();
        for (Future<?> f : futures) {
            try { f.get(); } catch (Exception ignored) {}
        }

        long dbCount = holdRepository.count();

        System.out.printf("%n  Successful creates : %d%n", createdIds.size());
        System.out.printf("  Errors             : %d%n", errors.size());
        System.out.printf("  Unique IDs in list : %d%n", createdIds.stream().distinct().count());
        System.out.printf("  Rows in DB         : %d%n", dbCount);
        errors.forEach(e -> System.out.printf("  Error: %s%n", e.getMessage()));

        // All workers must succeed
        assertThat(errors).as("No concurrent-create should throw").isEmpty();
        assertThat(createdIds).hasSize(workers);

        // All IDs must be distinct
        long distinctCount = createdIds.stream().distinct().count();
        assertThat(distinctCount)
                .as("All hold IDs must be unique; got %d distinct out of %d", distinctCount, workers)
                .isEqualTo(workers);

        // DB must contain at least 'workers' hold rows (some may persist from @BeforeEach seed)
        assertThat(dbCount).isGreaterThanOrEqualTo(workers);
    }
}
