"""
Concurrency test for cancel_booking() double-cancel race.

Two concurrent cancels of the same booking must not both pass the status check
and restore the seat twice.  Before the atomic fix this test FAILS because both
threads see status='booked', each execute the seat+1 UPDATE, and the seat counter
ends up 2 higher than expected (i.e. higher than INITIAL_SEATS).

Uses a file-based SQLite DB (not StaticPool / in-memory) so each thread gets a
genuinely independent connection — the same isolation failure as production.
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Make sure the package root is on sys.path so models/services are importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Base, Booking, Flight, User
from services.booking import cancel_booking
from schemas import ErrorResponse

INITIAL_SEATS = 1   # one economy seat; we'll book it then try to cancel twice
WORKERS = 2         # exactly two concurrent cancels of the same booking_id


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def file_engine(tmp_path_factory):
    """File-based SQLite engine; each worker thread creates its own Session."""
    db_path = tmp_path_factory.mktemp("race_cancel") / "cancel_race.db"
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
def seeded_booking(file_engine):
    """
    Seed one user, one flight (1 economy seat), one 'booked' booking.
    Yield (engine, booking_id, flight_id).
    Wipe rows after the test so the fixture can be reused.
    """
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=file_engine)
    session = SessionFactory()

    user = User(name="Alice", email="alice@galaxium.test")
    session.add(user)
    session.flush()

    flight = Flight(
        origin="Earth",
        destination="Mars",
        departure_time="2099-06-01 09:00",
        arrival_time="2099-06-08 09:00",
        base_price=500_000,
        economy_seats_available=INITIAL_SEATS,
        business_seats_available=0,
        galaxium_seats_available=0,
    )
    session.add(flight)
    session.flush()

    booking = Booking(
        user_id=user.user_id,
        flight_id=flight.flight_id,
        status="booked",
        booking_time=datetime.now(tz=timezone.utc).isoformat(),
        seat_class="economy",
        price_paid=500_000,
    )
    # The seat has been 'taken' by this booking, so decrement to reflect real state
    flight.economy_seats_available = 0
    session.add(booking)
    session.commit()

    booking_id = booking.booking_id
    flight_id = flight.flight_id
    session.close()

    yield file_engine, booking_id, flight_id

    # teardown
    cleanup = SessionFactory()
    cleanup.query(Booking).delete()
    cleanup.query(Flight).delete()
    cleanup.query(User).delete()
    cleanup.commit()
    cleanup.close()


# ── worker ────────────────────────────────────────────────────────────────────

def _cancel_worker(engine, booking_id: int):
    """Open a fresh session per worker — never share sessions across threads."""
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionFactory()
    try:
        return cancel_booking(db, booking_id=booking_id)
    finally:
        db.close()


# ── test ──────────────────────────────────────────────────────────────────────

class TestDoubleCancelRace:
    def test_concurrent_cancels_restore_seat_at_most_once(self, seeded_booking):
        """
        Two concurrent cancels of the same booking.

        Expected behaviour (correct):
          - Exactly ONE cancel succeeds (returns BookingOut).
          - Exactly ONE cancel is rejected with ALREADY_CANCELLED.
          - The flight's economy_seats_available ends up at exactly INITIAL_SEATS (1).

        What happens before the fix (buggy):
          - BOTH cancels see status='booked', pass the check, and execute seat+1.
          - economy_seats_available ends up at INITIAL_SEATS + 1 (= 2) — seats are
            fabricated out of thin air.
        """
        engine, booking_id, flight_id = seeded_booking
        SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        # Fire both futures before collecting — this is the simultaneous-start barrier.
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [
                pool.submit(_cancel_worker, engine, booking_id)
                for _ in range(WORKERS)
            ]
            results = [f.result() for f in as_completed(futures)]

        from schemas import BookingOut
        successes = [r for r in results if isinstance(r, BookingOut)]
        errors    = [r for r in results if isinstance(r, ErrorResponse)]

        # Re-read seat count with a fresh session to get committed DB state.
        check = SessionFactory()
        flight = check.query(Flight).filter_by(flight_id=flight_id).first()
        seats_after = flight.economy_seats_available
        check.close()

        print(f"\n  Successful cancels : {len(successes)}")
        print(f"  Rejected (error)   : {len(errors)}")
        print(f"  Seats after cancel : {seats_after}  (expected {INITIAL_SEATS})")
        print(f"  Seat leak          : {seats_after - INITIAL_SEATS}")

        assert len(successes) == 1, (
            f"Expected exactly 1 successful cancel, got {len(successes)}. "
            f"Errors: {[e.error_code for e in errors]}"
        )
        assert seats_after == INITIAL_SEATS, (
            f"Seat counter should be {INITIAL_SEATS} after one cancel, "
            f"but got {seats_after} — {seats_after - INITIAL_SEATS} extra seat(s) fabricated by the race"
        )
