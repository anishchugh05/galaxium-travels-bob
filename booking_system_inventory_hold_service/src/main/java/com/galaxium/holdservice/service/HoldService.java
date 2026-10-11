package com.galaxium.holdservice.service;

import com.galaxium.holdservice.client.PythonBackendClient;
import com.galaxium.holdservice.domain.AuditEvent;
import com.galaxium.holdservice.domain.Hold;
import com.galaxium.holdservice.domain.Quote;
import com.galaxium.holdservice.repository.AuditEventRepository;
import com.galaxium.holdservice.repository.HoldRepository;
import com.galaxium.holdservice.repository.QuoteRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

@Service
@RequiredArgsConstructor
@Slf4j
public class HoldService {

    private final HoldRepository holdRepository;
    private final QuoteRepository quoteRepository;
    private final AuditEventRepository auditEventRepository;
    private final PythonBackendClient pythonBackendClient;

    @Value("${hold.duration.minutes:15}")
    private int holdDurationMinutes;

    @Transactional
    public Hold createHold(String quoteId) {
        log.info("Creating hold for quote {}", quoteId);

        // Verify quote exists
        Quote quote = quoteRepository.findById(quoteId)
                .orElseThrow(() -> new IllegalArgumentException("Quote not found: " + quoteId));

        // Check if quote is expired
        if (quote.getExpiresAt().isBefore(Instant.now())) {
            throw new IllegalStateException("Quote has expired");
        }

        // Generate a collision-free hold ID using UUID.
        // The old count()+1 pattern races: two concurrent callers read the same count,
        // compute the same ID, and one gets a primary-key collision on save().
        String holdId = generateHoldId();

        // Create hold
        Hold hold = Hold.builder()
                .holdId(holdId)
                .quoteId(quoteId)
                .status(Hold.HoldStatus.HELD)
                .reservedUntil(Instant.now().plus(holdDurationMinutes, ChronoUnit.MINUTES))
                .build();

        hold = holdRepository.save(hold);

        // Audit event
        createAuditEvent("HOLD", holdId, "CREATED",
                String.format("Hold created for quote %s, expires at %s", quoteId, hold.getReservedUntil()));

        log.info("Hold {} created successfully", holdId);
        return hold;
    }

    @Transactional(readOnly = true)
    public Optional<Hold> getHold(String holdId) {
        return holdRepository.findById(holdId);
    }

    @Transactional
    public Hold confirmHold(String holdId) {
        log.info("Confirming hold {}", holdId);

        // Verify hold exists (needed to read quoteId later; no lock yet).
        Hold hold = holdRepository.findById(holdId)
                .orElseThrow(() -> new IllegalArgumentException("Hold not found: " + holdId));

        // Fast-path: already confirmed — idempotent return.
        if (hold.getStatus() == Hold.HoldStatus.CONFIRMED) {
            log.info("Hold {} already confirmed, returning existing booking reference", holdId);
            return hold;
        }

        // Atomically claim the hold: HELD → CONFIRMING.
        // Only ONE concurrent caller can win this UPDATE because the WHERE clause
        // requires status=HELD.  The second caller sees rowcount=0 and throws.
        // The reservedUntil > now guard also prevents confirming an expired hold.
        Instant now = Instant.now();
        int claimed = holdRepository.claimForConfirm(holdId, now);
        if (claimed == 0) {
            // Re-read to give a useful status in the error message.
            Hold current = holdRepository.findById(holdId)
                    .orElseThrow(() -> new IllegalArgumentException("Hold not found: " + holdId));
            if (current.getStatus() == Hold.HoldStatus.HELD) {
                // Status is still HELD but reservedUntil has passed.
                throw new IllegalStateException("Hold has expired");
            }
            throw new IllegalStateException("Hold is not in HELD status: " + current.getStatus());
        }

        // We exclusively own the CONFIRMING slot — reload to get fresh state.
        // Use a new variable (claimedHold) so 'hold' stays effectively final for lambdas above.
        final Hold claimedHold = holdRepository.findById(holdId)
                .orElseThrow(() -> new IllegalStateException("Hold disappeared after claim: " + holdId));

        // Get quote details
        Quote quote = quoteRepository.findById(claimedHold.getQuoteId())
                .orElseThrow(() -> new IllegalStateException("Quote not found: " + claimedHold.getQuoteId()));

        try {
            // Call Python backend to create booking.
            // Only ONE caller ever reaches this point for a given holdId.
            Map<String, Object> holdData = Map.of(
                    "travelerId", quote.getTravelerId(),
                    "travelerName", quote.getTravelerName(),
                    "flightId", quote.getFlightId(),
                    "seatClass", quote.getSeatClass()
            );

            PythonBackendClient.BookingResponse booking = pythonBackendClient.createBookingFromHold(holdData);

            // Transition CONFIRMING → CONFIRMED.
            claimedHold.setStatus(Hold.HoldStatus.CONFIRMED);
            claimedHold.setExternalBookingReference(String.valueOf(booking.getBookingId()));
            Hold confirmedHold = holdRepository.save(claimedHold);

            createAuditEvent("HOLD", holdId, "CONFIRMED",
                    String.format("Hold confirmed, booking ID: %s", booking.getBookingId()));

            log.info("Hold {} confirmed successfully with booking {}", holdId, booking.getBookingId());
            return confirmedHold;

        } catch (PythonBackendClient.BookingCreationException e) {
            log.error("Failed to create booking for hold {}", holdId, e);
            // Revert CONFIRMING → HELD so the caller can retry.
            claimedHold.setStatus(Hold.HoldStatus.HELD);
            claimedHold.setErrorMessage(e.getMessage());
            holdRepository.save(claimedHold);

            createAuditEvent("HOLD", holdId, "CONFIRMATION_FAILED", e.getMessage());

            throw new IllegalStateException("Failed to confirm hold: " + e.getMessage(), e);
        }
    }

    @Transactional
    public Hold releaseHold(String holdId) {
        log.info("Releasing hold {}", holdId);

        Hold hold = holdRepository.findById(holdId)
                .orElseThrow(() -> new IllegalArgumentException("Hold not found: " + holdId));

        if (hold.getStatus() != Hold.HoldStatus.HELD) {
            throw new IllegalStateException("Hold cannot be released, current status: " + hold.getStatus());
        }

        hold.setStatus(Hold.HoldStatus.RELEASED);
        hold = holdRepository.save(hold);

        createAuditEvent("HOLD", holdId, "RELEASED", "Hold manually released");

        log.info("Hold {} released successfully", holdId);
        return hold;
    }

    private String generateHoldId() {
        // UUID-based: collision-free under any concurrency, no DB round-trip required.
        // The old count()+1 pattern raced: two concurrent callers read the same count,
        // computed the same ID, and one got a primary-key violation on save().
        return "H-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12).toUpperCase();
    }

    private void createAuditEvent(String entityType, String entityId, String eventType, String details) {
        AuditEvent event = AuditEvent.builder()
                .entityType(entityType)
                .entityId(entityId)
                .eventType(eventType)
                .details(details)
                .build();
        auditEventRepository.save(event);
    }
}

// Made with Bob
