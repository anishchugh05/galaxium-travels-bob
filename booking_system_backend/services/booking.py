from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from models import Booking, Flight, User
from schemas import BookingOut, ErrorResponse, SeatClass

# Imported lazily inside cancel_booking to avoid a module-level circular import.
# waitlist.py imports models/schemas but not booking.py; booking.py imports
# attempt_promotion from waitlist.py — one-directional, no cycle.

# Price multipliers for each seat class
SEAT_CLASS_MULTIPLIERS = {
    'economy': 1.0,
    'business': 2.5,
    'galaxium': 5.0
}


def book_flight(db: Session, user_id: int, name: str, flight_id: int, seat_class: SeatClass = 'economy') -> BookingOut | ErrorResponse:
    """Book a seat on a specific flight for a user in the specified seat class."""
    # Validate seat class
    if seat_class not in SEAT_CLASS_MULTIPLIERS:
        return ErrorResponse(
            error="Invalid seat class",
            error_code="INVALID_SEAT_CLASS",
            details=f"Seat class '{seat_class}' is not valid. Valid options are: economy, business, galaxium."
        )
    
    # Check flight exists
    flight = db.query(Flight).filter(Flight.flight_id == flight_id).first()
    if not flight:
        return ErrorResponse(
            error="Flight not found",
            error_code="FLIGHT_NOT_FOUND",
            details=f"The specified flight_id {flight_id} does not exist in our system. Please check the flight_id or use list_flights to see available flights."
        )

    # Check user exists and name matches (before the atomic decrement to avoid
    # consuming a seat for an invalid user)
    user = db.query(User).filter(User.user_id == user_id, User.name == name).first()
    if not user:
        existing_user = db.query(User).filter(User.user_id == user_id).first()
        if existing_user:
            return ErrorResponse(
                error="Name mismatch",
                error_code="NAME_MISMATCH",
                details=f"User ID {user_id} exists but the name '{name}' does not match the registered name '{existing_user.name}'. Please verify the user's name or use the correct name for this user ID."
            )
        else:
            return ErrorResponse(
                error="User not found",
                error_code="USER_NOT_FOUND",
                details=f"User with ID {user_id} is not registered in our system. The user might need to register first, or you may need to check if the user_id is correct."
            )

    # Atomically decrement the seat counter only if a seat is still available.
    # Using a single UPDATE … WHERE seats > 0 eliminates the read-check-decrement
    # race: if two sessions race, only one will match the WHERE clause.
    seat_col = {
        'economy': Flight.economy_seats_available,
        'business': Flight.business_seats_available,
        'galaxium': Flight.galaxium_seats_available,
    }[seat_class]

    result = db.execute(
        update(Flight)
        .where(Flight.flight_id == flight_id, seat_col > 0)
        .values({seat_col.key: seat_col - 1})
        .execution_options(synchronize_session=False)
    )

    if result.rowcount == 0:
        return ErrorResponse(
            error=f"No {seat_class} seats available",
            error_code="NO_SEATS_AVAILABLE",
            details=f"The flight has no available seats in {seat_class} class. Please try a different class or check other flights."
        )

    # Calculate price based on seat class (flight row already fetched above)
    price_paid = int(flight.base_price * SEAT_CLASS_MULTIPLIERS[seat_class])

    new_booking = _create_booking(db, user_id, flight_id, seat_class, price_paid)
    db.commit()
    db.refresh(new_booking)
    return BookingOut.model_validate(new_booking)


def _create_booking(db: Session, user_id: int, flight_id: int, seat_class: str, price_paid: int) -> Booking:
    """Append a new Booking to the session WITHOUT committing.

    Callers are responsible for calling db.commit() and db.refresh() afterwards.
    This separation allows attempt_promotion() to add the booking to the same
    transaction as the waitlist status update and the cancel status change,
    so all three writes land in a single atomic commit.
    """
    new_booking = Booking(
        user_id=user_id,
        flight_id=flight_id,
        status="booked",
        booking_time=datetime.now(tz=timezone.utc).isoformat(),
        seat_class=seat_class,
        price_paid=price_paid,
    )
    db.add(new_booking)
    return new_booking


def cancel_booking(db: Session, booking_id: int) -> BookingOut | ErrorResponse:
    """Cancel an existing booking by its booking_id and restore seat to correct class."""
    booking = db.query(Booking).filter(Booking.booking_id == booking_id).first()
    if not booking:
        return ErrorResponse(
            error="Booking not found",
            error_code="BOOKING_NOT_FOUND",
            details=f"Booking with ID {booking_id} not found. The booking may have been deleted or the booking_id may be incorrect. Please verify the booking_id or check if the booking exists."
        )

    if booking.status == "cancelled":
        return ErrorResponse(
            error="Booking already cancelled",
            error_code="ALREADY_CANCELLED",
            details=f"Booking {booking_id} is already cancelled and cannot be cancelled again. The booking status is currently '{booking.status}'. If you need to make changes, please contact support."
        )

    # Resolve the seat column before touching any state.
    # An unrecognised seat_class means data corruption; refuse to cancel
    # rather than silently lose a seat from inventory.
    seat_col = {
        'economy': Flight.economy_seats_available,
        'business': Flight.business_seats_available,
        'galaxium': Flight.galaxium_seats_available,
    }.get(booking.seat_class)

    if seat_col is None:
        return ErrorResponse(
            error="Invalid seat class on booking",
            error_code="INVALID_SEAT_CLASS",
            details=f"Booking {booking_id} has unrecognised seat class '{booking.seat_class}'. "
                    f"Cannot safely restore inventory. Cancellation refused."
        )

    # Atomically restore the seat counter.
    db.execute(
        update(Flight)
        .where(Flight.flight_id == booking.flight_id)
        .values({seat_col.key: seat_col + 1})
        .execution_options(synchronize_session=False)
    )

    # Attempt to promote the first waiting user for this flight+class.
    # All three writes (seat+1, promotion booking, waitlist status) will be
    # covered by the single db.commit() below — nothing is committed yet.
    from services.waitlist import attempt_promotion  # noqa: PLC0415 (local import, avoids module-level cycle)
    attempt_promotion(db, booking.flight_id, booking.seat_class)

    booking.status = "cancelled"
    db.commit()
    db.refresh(booking)
    return BookingOut.model_validate(booking)


def get_bookings(db: Session, user_id: int) -> list[BookingOut]:
    """Retrieve all bookings for a specific user."""
    bookings = db.query(Booking).filter(Booking.user_id == user_id).all()
    return [BookingOut.model_validate(b) for b in bookings]
