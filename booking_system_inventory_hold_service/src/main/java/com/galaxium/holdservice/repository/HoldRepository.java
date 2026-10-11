package com.galaxium.holdservice.repository;

import com.galaxium.holdservice.domain.Hold;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.stereotype.Repository;

import java.time.Instant;

@Repository
public interface HoldRepository extends JpaRepository<Hold, String> {

    /**
     * Atomically transition a single hold from HELD → CONFIRMING.
     * Returns the number of rows updated (1 = claimed, 0 = already claimed or gone).
     * The WHERE clause is the guard; only ONE concurrent caller can match it.
     */
    @Modifying
    @Query("UPDATE Hold h SET h.status = com.galaxium.holdservice.domain.Hold$HoldStatus.CONFIRMING, h.updatedAt = :now " +
           "WHERE h.holdId = :holdId AND h.status = com.galaxium.holdservice.domain.Hold$HoldStatus.HELD " +
           "AND h.reservedUntil > :now")
    int claimForConfirm(String holdId, Instant now);

    /**
     * Atomically expire all HELD holds whose reservedUntil has passed.
     * Returns the count of rows updated.
     * Replaces the load-then-loop pattern in HoldExpirationScheduler, eliminating
     * the race between the scheduler and a concurrent confirmHold() call.
     */
    @Modifying
    @Query("UPDATE Hold h SET h.status = com.galaxium.holdservice.domain.Hold$HoldStatus.EXPIRED, h.updatedAt = :now " +
           "WHERE h.status = com.galaxium.holdservice.domain.Hold$HoldStatus.HELD AND h.reservedUntil < :now")
    int expireHeldHolds(Instant now);
}

// Made with Bob
