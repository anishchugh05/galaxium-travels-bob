package com.galaxium.holdservice.service;

import com.galaxium.holdservice.api.dto.CreateQuoteRequest;
import com.galaxium.holdservice.domain.AuditEvent;
import com.galaxium.holdservice.domain.Quote;
import com.galaxium.holdservice.repository.AuditEventRepository;
import com.galaxium.holdservice.repository.QuoteRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Optional;
import java.util.UUID;

@Service
@RequiredArgsConstructor
@Slf4j
public class QuoteService {

    private final QuoteRepository quoteRepository;
    private final AuditEventRepository auditEventRepository;
    private final PricingService pricingService;

    @Transactional
    public Quote createQuote(CreateQuoteRequest request) {
        log.info("Creating quote for flight {} with {} {} seats", 
                request.getFlightId(), request.getQuantity(), request.getSeatClass());

        // Generate quote ID
        String quoteId = generateQuoteId();

        // Calculate pricing
        long pricePerSeat = pricingService.calculatePrice(
                request.getFlightId(), 
                request.getSeatClass()
        );
        long totalPrice = pricePerSeat * request.getQuantity();

        // Create quote
        Quote quote = Quote.builder()
                .quoteId(quoteId)
                .flightId(request.getFlightId())
                .seatClass(request.getSeatClass())
                .quantity(request.getQuantity())
                .travelerId(request.getTravelerId())
                .travelerName(request.getTravelerName())
                .pricePerSeat(pricePerSeat)
                .totalPrice(totalPrice)
                .expiresAt(Instant.now().plus(24, ChronoUnit.HOURS))
                .status(Quote.QuoteStatus.CREATED)
                .build();

        quote = quoteRepository.save(quote);

        // Audit event
        createAuditEvent("QUOTE", quoteId, "CREATED", 
                String.format("Quote created for flight %d, %d %s seats", 
                        request.getFlightId(), request.getQuantity(), request.getSeatClass()));

        log.info("Quote {} created successfully", quoteId);
        return quote;
    }

    @Transactional(readOnly = true)
    public Optional<Quote> getQuote(String quoteId) {
        return quoteRepository.findById(quoteId);
    }

    private String generateQuoteId() {
        // UUID-based: collision-free under any concurrency, no DB round-trip required.
        // The old count()+1 pattern raced: two concurrent callers read the same count,
        // computed the same ID, and one got a primary-key violation on save().
        return "Q-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12).toUpperCase();
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
