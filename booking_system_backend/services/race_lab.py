"""Race Lab demo service.

Creates / resets a dedicated demo flight with exactly 5 economy seats and
ensures 20 demo users (racer01..racer20) exist.  Touches nothing else.

The demo flight is identified by the sentinel tag in its origin field:
  origin = "__race_lab__"
This lets list_flights filter it out from the normal Flights page.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import Booking, Flight, User
from schemas import UserOut

# Sentinel used to identify the demo flight
DEMO_FLIGHT_ORIGIN = "__race_lab__"


def _get_or_create_demo_flight(db: Session) -> Flight:
    """Return the existing demo flight, or create it if absent."""
    f = db.query(Flight).filter(Flight.origin == DEMO_FLIGHT_ORIGIN).first()
    if f is None:
        f = Flight(
            origin=DEMO_FLIGHT_ORIGIN,
            destination="Race Lab Station",
            departure_time="2099-12-31 00:00",
            arrival_time="2099-12-31 12:00",
            base_price=1,
            economy_seats_available=5,
            business_seats_available=0,
            galaxium_seats_available=0,
        )
        db.add(f)
        db.flush()  # assign flight_id
    else:
        # Reset seat count to exactly 5 economy
        f.economy_seats_available = 5
        f.business_seats_available = 0
        f.galaxium_seats_available = 0
    return f


def _ensure_racer_users(db: Session) -> list[User]:
    """Ensure racer01..racer20 exist.  Returns all 20 in order."""
    users: list[User] = []
    for i in range(1, 21):
        name = f"racer{i:02d}"
        email = f"{name}@race-lab.demo"
        u = db.query(User).filter(User.email == email).first()
        if u is None:
            u = User(name=name, email=email)
            db.add(u)
            db.flush()
        users.append(u)
    return users


def reset_race_lab(db: Session) -> dict:
    """Idempotent reset: recreate the demo flight at 5 seats, delete its
    previous bookings, and ensure 20 racer users exist.

    Returns:
        {
            "flight_id": int,
            "users": [{"user_id": int, "name": str, "email": str}, ...]
        }
    """
    demo_flight = _get_or_create_demo_flight(db)

    # Delete only bookings that belong to this demo flight
    db.query(Booking).filter(Booking.flight_id == demo_flight.flight_id).delete(
        synchronize_session="fetch"
    )

    users = _ensure_racer_users(db)
    db.commit()
    db.refresh(demo_flight)

    return {
        "flight_id": demo_flight.flight_id,
        "users": [
            UserOut(user_id=u.user_id, name=u.name, email=u.email).model_dump()
            for u in users
        ],
    }
