package com.galaxium.holdservice.scheduler;

import com.galaxium.holdservice.repository.HoldRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;

@Component
@RequiredArgsConstructor
@Slf4j
public class HoldExpirationScheduler {

    private final HoldRepository holdRepository;

    /**
     * Atomically expire all HELD holds whose reservedUntil has passed.
     *
     * Uses a single conditional UPDATE … WHERE status=HELD AND reservedUntil < :now
     * rather than the previous load-then-loop pattern.  This eliminates the race
     * between the scheduler and a concurrent confirmHold() call:
     *
     *   Old code: scheduler SELECTs hold (status=HELD), confirmHold() also sees HELD,
     *             scheduler writes EXPIRED, confirmHold() writes CONFIRMED — last writer wins.
     *
     *   New code: confirmHold() atomically claims HELD→CONFIRMING before calling Python.
     *             The scheduler's UPDATE only matches status=HELD, so it cannot touch a
     *             hold that is already in CONFIRMING or CONFIRMED.
     *
     * Audit events are no longer written per-row here because the UPDATE is a bulk
     * statement with no row handles; the confirm path still writes its own audit events.
     */
    @Scheduled(fixedDelayString = "${hold.expiration.check.interval.seconds:60}000")
    @Transactional
    public void expireHolds() {
        Instant now = Instant.now();
        int expired = holdRepository.expireHeldHolds(now);
        if (expired > 0) {
            log.info("Expired {} held hold(s) at {}", expired, now);
        }
    }
}

// Made with Bob
