"""
Concurrency tests for the atomic seat-management operations in book_flight() and
cancel_booking().

Uses a file-based SQLite DB (not in-memory / StaticPool) so each thread opens a
genuinely independent connection — the same isolation failure mode as production.
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Base, Booking, Flight, User
from schemas import BookingOut, ErrorResponse
from services import booking as booking_service

ECONOMY_SEATS = 5
CONCURRENT_REQUESTS = 20


@pytest.fixture(scope="module")
def file_db(tmp_path_factory):
    """
    File-based SQLite engine shared across the module.
    Each thread creates its own Session from the same sessionmaker so they get
    separate connections — reproducing the production isolation failure.
    """
    db_path = tmp_path_factory.mktemp("concurrency") / "race_test.db"
    db_url = f"sqlite:///{db_path}"

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        # NullPool would force a new connection per call; the default pool is
        # fine here — what matters is that sessions are separate objects.
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()
    if db_path.exists():
        db_path.unlink()


@pytest.fixture()
def seeded_db(file_db):
    """
    Seed a single flight with ECONOMY_SEATS seats and 20 distinct users,
    then drop the rows after each test so the fixture is reusable.
    """
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=file_db)
    session = SessionLocal()

    flight = Flight(
        origin="Earth",
        destination="Mars",
        departure_time="2099-06-01 10:00",
        arrival_time="2099-06-01 18:00",
        base_price=500_000,
        economy_seats_available=ECONOMY_SEATS,
        business_seats_available=0,
        galaxium_seats_available=0,
    )
    session.add(flight)
    session.flush()  # get flight.flight_id

    users = [
        User(name=f"Traveller {i}", email=f"traveller{i}@galaxy.test")
        for i in range(CONCURRENT_REQUESTS)
    ]
    session.add_all(users)
    session.commit()

    flight_id = flight.flight_id
    user_ids = [(u.user_id, u.name) for u in users]

    session.close()

    yield file_db, flight_id, user_ids

    # Teardown: wipe rows so the next test starts clean
    cleanup = SessionLocal()
    cleanup.query(Booking).delete()
    cleanup.query(Flight).delete()
    cleanup.query(User).delete()
    cleanup.commit()
    cleanup.close()


def _attempt_booking(engine, user_id: int, name: str, flight_id: int) -> BookingOut | ErrorResponse:
    """Called from a worker thread — opens its own session."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        return booking_service.book_flight(db, user_id=user_id, name=name, flight_id=flight_id)
    finally:
        db.close()


class TestOversellRace:
    def test_concurrent_bookings_do_not_oversell(self, seeded_db):
        """
        Fire CONCURRENT_REQUESTS simultaneous bookings against a flight that only
        has ECONOMY_SEATS seats.  Without any locking, more than ECONOMY_SEATS
        bookings will succeed — demonstrating the race condition.

        This test is EXPECTED TO FAIL until the race is fixed.
        """
        engine, flight_id, user_ids = seeded_db
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        results = []
        with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as pool:
            futures = [
                pool.submit(_attempt_booking, engine, uid, name, flight_id)
                for uid, name in user_ids
            ]
            for fut in as_completed(futures):
                results.append(fut.result())

        successes = [r for r in results if isinstance(r, BookingOut)]
        errors = [r for r in results if isinstance(r, ErrorResponse)]

        # Report what actually happened
        final_db = SessionLocal()
        final_flight = final_db.query(Flight).filter(Flight.flight_id == flight_id).first()
        seats_left = final_flight.economy_seats_available
        final_db.close()

        print(f"\n  Successful bookings : {len(successes)}")
        print(f"  Rejected bookings   : {len(errors)}")
        print(f"  Seats remaining     : {seats_left}")
        print(f"  Oversold by         : {max(0, len(successes) - ECONOMY_SEATS)}")

        # These two assertions define correct behaviour.
        # Both are expected to FAIL before the race condition is fixed.
        assert len(successes) == ECONOMY_SEATS, (
            f"Expected exactly {ECONOMY_SEATS} successful bookings, got {len(successes)} — "
            f"flight was oversold by {len(successes) - ECONOMY_SEATS} seat(s)"
        )
        assert seats_left == 0, (
            f"Expected 0 seats remaining, got {seats_left}"
        )

CANCEL_BOOKINGS = 10


@pytest.fixture()
def seeded_db_for_cancel(file_db):
    """
    Seed a flight with 0 seats (all sold) and 10 pre-existing bookings so
    cancel_booking() has something to cancel.  Each booking belongs to a
    distinct user to avoid any per-user serialisation.
    """
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=file_db)
    session = SessionLocal()

    flight = Flight(
        origin="Jupiter",
        destination="Saturn",
        departure_time="2099-07-01 08:00",
        arrival_time="2099-07-01 20:00",
        base_price=750_000,
        economy_seats_available=0,   # all sold out
        business_seats_available=0,
        galaxium_seats_available=0,
    )
    session.add(flight)
    session.flush()

    users = [
        User(name=f"Canceller {i}", email=f"canceller{i}@galaxy.test")
        for i in range(CANCEL_BOOKINGS)
    ]
    session.add_all(users)
    session.flush()

    from datetime import datetime, timezone
    bookings = [
        Booking(
            user_id=u.user_id,
            flight_id=flight.flight_id,
            status="booked",
            booking_time=datetime.now(tz=timezone.utc).isoformat(),
            seat_class="economy",
            price_paid=750_000,
        )
        for u in users
    ]
    session.add_all(bookings)
    session.commit()

    flight_id = flight.flight_id
    booking_ids = [b.booking_id for b in bookings]

    session.close()

    yield file_db, flight_id, booking_ids

    # Teardown
    cleanup = SessionLocal()
    cleanup.query(Booking).delete()
    cleanup.query(Flight).delete()
    cleanup.query(User).delete()
    cleanup.commit()
    cleanup.close()


def _attempt_cancel(engine, booking_id: int) -> BookingOut | ErrorResponse:
    """Called from a worker thread — opens its own session."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        return booking_service.cancel_booking(db, booking_id=booking_id)
    finally:
        db.close()


class TestCancelRace:
    def test_concurrent_cancels_restore_all_seats(self, seeded_db_for_cancel):
        """
        Cancel CANCEL_BOOKINGS bookings concurrently on a sold-out flight and
        assert that seats_available is restored by exactly CANCEL_BOOKINGS.

        Without atomic UPDATE the lost-update race causes some increments to
        overwrite each other, leaving the counter short.
        """
        engine, flight_id, booking_ids = seeded_db_for_cancel
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        results = []
        with ThreadPoolExecutor(max_workers=CANCEL_BOOKINGS) as pool:
            futures = [
                pool.submit(_attempt_cancel, engine, bid)
                for bid in booking_ids
            ]
            for fut in as_completed(futures):
                results.append(fut.result())

        successes = [r for r in results if isinstance(r, BookingOut)]
        errors = [r for r in results if isinstance(r, ErrorResponse)]

        final_db = SessionLocal()
        final_flight = final_db.query(Flight).filter(Flight.flight_id == flight_id).first()
        seats_restored = final_flight.economy_seats_available
        final_db.close()

        print(f"\n  Successful cancels  : {len(successes)}")
        print(f"  Errors              : {len(errors)}")
        print(f"  Seats restored      : {seats_restored}")
        print(f"  Under-restored by   : {CANCEL_BOOKINGS - seats_restored}")

        assert len(successes) == CANCEL_BOOKINGS, (
            f"Expected all {CANCEL_BOOKINGS} cancels to succeed, got {len(successes)}"
        )
        assert seats_restored == CANCEL_BOOKINGS, (
            f"Expected {CANCEL_BOOKINGS} seats restored, got {seats_restored} — "
            f"lost-update race dropped {CANCEL_BOOKINGS - seats_restored} increment(s)"
        )
