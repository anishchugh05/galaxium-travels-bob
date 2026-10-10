"""Waitlist service — per-flight, per-seat-class queue.

Promotion design
----------------
When cancel_booking() restores a seat it calls attempt_promotion() *before*
committing.  attempt_promotion() loops through the queue atomically:

  1. Claim the next 'waiting' entry by flipping it to 'promoting' with a
     single UPDATE … WHERE waitlist_id = (SELECT MIN(…) WHERE status='waiting' …).
     SQLite serialises writes, so only one concurrent caller can claim a given
     entry — rowcount == 0 means someone else got there first or the queue is empty.

  2. Atomically decrement the flight seat counter (same pattern as book_flight()).
     If rowcount == 0 the seat was grabbed by a direct booker racing us:
       → reset entry to 'waiting' and exit (the seat is gone; nothing to promote).

  3. On success: call _create_booking() (no commit — caller owns the commit),
     mark entry 'booked', exit.

  4. On any other exception: mark entry 'failed' and loop to try the next waiter.

All mutations share the single db.commit() in cancel_booking(), so the seat
restore, the new booking, and the waitlist status change are atomic.
"""
from datetime import datetime, timezone

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from models import Booking, Flight, User, Waitlist
from schemas import ErrorResponse, SeatClass, WaitlistOut, WaitlistRequest

# Reuse the same price multipliers as booking.py without importing that module
# (would create a circular import: booking → waitlist → booking).
_SEAT_CLASS_MULTIPLIERS = {
    'economy': 1.0,
    'business': 2.5,
    'galaxium': 5.0,
}

_SEAT_COLS = {
    'economy': Flight.economy_seats_available,
    'business': Flight.business_seats_available,
    'galaxium': Flight.galaxium_seats_available,
}


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def join_waitlist(
    db: Session,
    user_id: int,
    name: str,
    flight_id: int,
    seat_class: SeatClass,
) -> WaitlistOut | ErrorResponse:
    """Add a user to the waitlist for a specific flight and seat class."""

    if seat_class not in _SEAT_CLASS_MULTIPLIERS:
        return ErrorResponse(
            error="Invalid seat class",
            error_code="INVALID_SEAT_CLASS",
            details=f"Seat class '{seat_class}' is not valid. Valid options are: economy, business, galaxium.",
        )

    flight = db.query(Flight).filter(Flight.flight_id == flight_id).first()
    if not flight:
        return ErrorResponse(
            error="Flight not found",
            error_code="FLIGHT_NOT_FOUND",
            details=f"Flight {flight_id} does not exist.",
        )

    # Validate user identity (same check as book_flight).
    user = db.query(User).filter(User.user_id == user_id, User.name == name).first()
    if not user:
        existing = db.query(User).filter(User.user_id == user_id).first()
        if existing:
            return ErrorResponse(
                error="Name mismatch",
                error_code="NAME_MISMATCH",
                details=f"User ID {user_id} exists but name '{name}' does not match registered name '{existing.name}'.",
            )
        return ErrorResponse(
            error="User not found",
            error_code="USER_NOT_FOUND",
            details=f"User {user_id} is not registered.",
        )

    # Guard: joining when seats are available makes no sense.
    seats_available = getattr(flight, _SEAT_COLS[seat_class].key)
    if seats_available > 0:
        return ErrorResponse(
            error=f"{seat_class.capitalize()} seats are available",
            error_code="SEATS_AVAILABLE",
            details=f"There are still {seats_available} {seat_class} seat(s) available. Book directly instead of joining the waitlist.",
        )

    # Guard: duplicate active entry (enforced by partial unique index, but give a
    # readable error before hitting the DB constraint).
    existing_entry = db.query(Waitlist).filter(
        Waitlist.user_id == user_id,
        Waitlist.flight_id == flight_id,
        Waitlist.seat_class == seat_class,
        Waitlist.status == 'waiting',
    ).first()
    if existing_entry:
        return ErrorResponse(
            error="Already on waitlist",
            error_code="ALREADY_ON_WAITLIST",
            details=f"User {user_id} is already waiting for {seat_class} on flight {flight_id}.",
        )

    entry = Waitlist(
        user_id=user_id,
        flight_id=flight_id,
        seat_class=seat_class,
        status='waiting',
        added_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return WaitlistOut.model_validate(entry)


def get_waitlist_for_user(db: Session, user_id: int) -> list[WaitlistOut]:
    """Return all active (non-cancelled, non-failed) waitlist entries for a user."""
    entries = db.query(Waitlist).filter(
        Waitlist.user_id == user_id,
        Waitlist.status.notin_(['cancelled', 'failed']),
    ).all()
    return [WaitlistOut.model_validate(e) for e in entries]


def leave_waitlist(
    db: Session,
    waitlist_id: int,
    user_id: int,
) -> WaitlistOut | ErrorResponse:
    """Remove the current user from the waitlist."""
    entry = db.query(Waitlist).filter(Waitlist.waitlist_id == waitlist_id).first()
    if not entry:
        return ErrorResponse(
            error="Waitlist entry not found",
            error_code="WAITLIST_ENTRY_NOT_FOUND",
            details=f"No waitlist entry with ID {waitlist_id}.",
        )

    if entry.user_id != user_id:
        return ErrorResponse(
            error="Not your waitlist entry",
            error_code="NOT_YOUR_ENTRY",
            details=f"Waitlist entry {waitlist_id} belongs to a different user.",
        )

    if entry.status != 'waiting':
        return ErrorResponse(
            error="Cannot leave waitlist",
            error_code="CANNOT_LEAVE",
            details=f"Entry {waitlist_id} has status '{entry.status}' and cannot be cancelled. Only 'waiting' entries can be left.",
        )

    entry.status = 'cancelled'
    db.commit()
    db.refresh(entry)
    return WaitlistOut.model_validate(entry)


# ---------------------------------------------------------------------------
# Internal promotion helper — called by cancel_booking(), no commit inside
# ---------------------------------------------------------------------------

def attempt_promotion(db: Session, flight_id: int, seat_class: str) -> None:
    """Try to promote the first waiter for flight_id/seat_class.

    Must be called BEFORE db.commit() inside cancel_booking() so that the new
    Booking row and the Waitlist status change are committed atomically with the
    cancel's seat-restore UPDATE.

    Loop strategy:
    - Claim next 'waiting' entry atomically (MIN subquery, not ORDER BY/LIMIT —
      SQLite's default build does not support UPDATE ... ORDER BY).
    - If no waiter: exit.
    - Atomically decrement seat (same pattern as book_flight):
        rowcount == 0 → seat grabbed by racing direct booker; reset entry to
                        'waiting' and exit (no point promoting; seat is gone).
        rowcount == 1 → seat secured; create the booking, mark entry 'booked', exit.
    - On unexpected exception: mark entry 'failed', loop to try next waiter.
    """
    seat_col = _SEAT_COLS.get(seat_class)
    if seat_col is None:
        # Unknown seat class — nothing to promote.
        return

    while True:
        # --- Step 1: find the earliest 'waiting' entry id ---------------------
        next_id = db.execute(
            text(
                "SELECT MIN(waitlist_id) FROM waitlist "
                "WHERE status = 'waiting' AND flight_id = :fid AND seat_class = :sc"
            ),
            {'fid': flight_id, 'sc': seat_class},
        ).scalar()

        if next_id is None:
            # No 'waiting' entries — nothing to promote.
            break

        # --- Step 2: atomically claim that exact entry by id ------------------
        # The outer WHERE targets the specific id returned by the subquery,
        # so concurrent sessions that already claimed it see rowcount == 0.
        claim_result = db.execute(
            update(Waitlist)
            .where(Waitlist.waitlist_id == next_id, Waitlist.status == 'waiting')
            .values(status='promoting')
            .execution_options(synchronize_session=False)
        )

        if claim_result.rowcount == 0:
            # Another session claimed this entry first — retry from the top to
            # find the next available waiter.
            continue

        # --- Step 3: look up the entry we just claimed by its exact id --------
        entry = db.query(Waitlist).filter(Waitlist.waitlist_id == next_id).first()

        if entry is None:
            # Should not happen, but be defensive.
            break

        # --- Step 3: look up the user's current name from the DB ---------------
        user = db.query(User).filter(User.user_id == entry.user_id).first()
        if user is None:
            # User was deleted after joining the waitlist — skip.
            entry.status = 'failed'
            continue

        # --- Step 4: atomically decrement the seat ----------------------------
        try:
            decrement = db.execute(
                update(Flight)
                .where(Flight.flight_id == flight_id, seat_col > 0)
                .values({seat_col.key: seat_col - 1})
                .execution_options(synchronize_session=False)
            )

            if decrement.rowcount == 0:
                # Seat was grabbed by a direct booker racing this cancel.
                # Reset entry to 'waiting' so the user stays in the queue for
                # the next cancellation.
                entry.status = 'waiting'
                break

            # --- Step 5: create the booking (no commit — caller commits) ------
            price_paid = int(
                db.query(Flight.base_price)
                .filter(Flight.flight_id == flight_id)
                .scalar()
                * _SEAT_CLASS_MULTIPLIERS[seat_class]
            )
            from services.booking import _create_booking  # local import avoids cycle
            _create_booking(db, entry.user_id, flight_id, seat_class, price_paid)
            entry.status = 'booked'
            break

        except Exception:
            # Unexpected error promoting this entry — mark failed, try next waiter.
            entry.status = 'failed'
            continue
