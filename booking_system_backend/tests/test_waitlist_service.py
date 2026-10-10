"""Unit tests for the waitlist service and promotion integration.

All tests use the in-memory db_session fixture from conftest.py.
The Waitlist table is created automatically because Waitlist is now in
Base.metadata (models.py) — conftest calls Base.metadata.create_all().
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Booking, Flight, User, Waitlist
from schemas import ErrorResponse, WaitlistOut
from services import booking as booking_service
from services import waitlist as waitlist_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(db, name="Alice", email="alice@galaxy.test"):
    u = User(name=name, email=email)
    db.add(u)
    db.flush()
    return u


def _make_flight(db, economy=0, business=0, galaxium=0):
    f = Flight(
        origin="Earth", destination="Mars",
        departure_time="2099-06-01 10:00", arrival_time="2099-06-01 18:00",
        base_price=500_000,
        economy_seats_available=economy,
        business_seats_available=business,
        galaxium_seats_available=galaxium,
    )
    db.add(f)
    db.flush()
    return f


def _make_booking(db, user, flight, seat_class="economy", status="booked"):
    b = Booking(
        user_id=user.user_id,
        flight_id=flight.flight_id,
        status=status,
        booking_time="2099-01-01T00:00:00+00:00",
        seat_class=seat_class,
        price_paid=500_000,
    )
    db.add(b)
    db.flush()
    return b


def _make_waitlist_entry(db, user, flight, seat_class="economy", status="waiting"):
    from datetime import datetime, timezone
    e = Waitlist(
        user_id=user.user_id,
        flight_id=flight.flight_id,
        seat_class=seat_class,
        status=status,
        added_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    db.add(e)
    db.flush()
    return e


# ---------------------------------------------------------------------------
# join_waitlist
# ---------------------------------------------------------------------------

class TestJoinWaitlist:

    def test_join_waitlist_success(self, db_session):
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        result = waitlist_service.join_waitlist(db_session, u.user_id, u.name, f.flight_id, "economy")

        assert isinstance(result, WaitlistOut)
        assert result.status == "waiting"
        assert result.user_id == u.user_id
        assert result.flight_id == f.flight_id
        assert result.seat_class == "economy"

    def test_join_waitlist_seats_available(self, db_session):
        """Can't join when seats are still open."""
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=3)
        db_session.commit()

        result = waitlist_service.join_waitlist(db_session, u.user_id, u.name, f.flight_id, "economy")

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "SEATS_AVAILABLE"

    def test_join_waitlist_already_on_list(self, db_session):
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        waitlist_service.join_waitlist(db_session, u.user_id, u.name, f.flight_id, "economy")
        result = waitlist_service.join_waitlist(db_session, u.user_id, u.name, f.flight_id, "economy")

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "ALREADY_ON_WAITLIST"

    def test_join_waitlist_user_not_found(self, db_session):
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        result = waitlist_service.join_waitlist(db_session, 9999, "Ghost", f.flight_id, "economy")

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "USER_NOT_FOUND"

    def test_join_waitlist_name_mismatch(self, db_session):
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        result = waitlist_service.join_waitlist(db_session, u.user_id, "Wrong Name", f.flight_id, "economy")

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "NAME_MISMATCH"

    def test_join_waitlist_flight_not_found(self, db_session):
        u = _make_user(db_session)
        db_session.commit()

        result = waitlist_service.join_waitlist(db_session, u.user_id, u.name, 9999, "economy")

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "FLIGHT_NOT_FOUND"

    def test_join_waitlist_rejoin_after_cancel(self, db_session):
        """Partial unique index allows rejoining once the old entry is no longer 'waiting'."""
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        # Join then leave
        entry = waitlist_service.join_waitlist(db_session, u.user_id, u.name, f.flight_id, "economy")
        waitlist_service.leave_waitlist(db_session, entry.waitlist_id, u.user_id)

        # Rejoin — must succeed
        result = waitlist_service.join_waitlist(db_session, u.user_id, u.name, f.flight_id, "economy")

        assert isinstance(result, WaitlistOut)
        assert result.status == "waiting"


# ---------------------------------------------------------------------------
# get_waitlist_for_user
# ---------------------------------------------------------------------------

class TestGetWaitlist:

    def test_get_waitlist_empty(self, db_session):
        assert waitlist_service.get_waitlist_for_user(db_session, 1) == []

    def test_get_waitlist_excludes_cancelled(self, db_session):
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        e = _make_waitlist_entry(db_session, u, f, status='waiting')
        _make_waitlist_entry(db_session, u, f, status='cancelled')  # second entry — different status
        # Note: two entries for same user/flight/class works here because only one is 'waiting'
        # (partial unique index only blocks duplicates with status='waiting').
        # To avoid the unique index violation we mark the first one cancelled before adding second.
        e_obj = db_session.query(Waitlist).filter(Waitlist.waitlist_id == e.waitlist_id).first()
        e_obj.status = 'cancelled'
        db_session.flush()
        _make_waitlist_entry(db_session, u, f, status='waiting')
        db_session.commit()

        results = waitlist_service.get_waitlist_for_user(db_session, u.user_id)

        assert len(results) == 1
        assert results[0].status == "waiting"

    def test_get_waitlist_returns_waiting_entries(self, db_session):
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        _make_waitlist_entry(db_session, u, f)
        db_session.commit()

        results = waitlist_service.get_waitlist_for_user(db_session, u.user_id)

        assert len(results) == 1
        assert results[0].user_id == u.user_id


# ---------------------------------------------------------------------------
# leave_waitlist
# ---------------------------------------------------------------------------

class TestLeaveWaitlist:

    def test_leave_waitlist_success(self, db_session):
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        entry = waitlist_service.join_waitlist(db_session, u.user_id, u.name, f.flight_id, "economy")
        result = waitlist_service.leave_waitlist(db_session, entry.waitlist_id, u.user_id)

        assert isinstance(result, WaitlistOut)
        assert result.status == "cancelled"

        db_entry = db_session.query(Waitlist).filter(Waitlist.waitlist_id == entry.waitlist_id).first()
        assert db_entry.status == "cancelled"

    def test_leave_waitlist_not_found(self, db_session):
        result = waitlist_service.leave_waitlist(db_session, 9999, 1)

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "WAITLIST_ENTRY_NOT_FOUND"

    def test_leave_waitlist_wrong_user(self, db_session):
        u1 = _make_user(db_session, name="Alice", email="alice@g.test")
        u2 = _make_user(db_session, name="Bob", email="bob@g.test")
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        entry = waitlist_service.join_waitlist(db_session, u1.user_id, u1.name, f.flight_id, "economy")
        result = waitlist_service.leave_waitlist(db_session, entry.waitlist_id, u2.user_id)

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "NOT_YOUR_ENTRY"

    def test_leave_waitlist_already_promoted(self, db_session):
        """Cannot leave an entry that is already 'booked'."""
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=0)
        db_session.commit()

        e = _make_waitlist_entry(db_session, u, f, status='booked')
        db_session.commit()

        result = waitlist_service.leave_waitlist(db_session, e.waitlist_id, u.user_id)

        assert isinstance(result, ErrorResponse)
        assert result.error_code == "CANNOT_LEAVE"


# ---------------------------------------------------------------------------
# Promotion integration — triggered via cancel_booking()
# ---------------------------------------------------------------------------

class TestPromotionUnit:

    def test_cancel_promotes_waitlisted_user(self, db_session):
        """Cancel a booking → waitlisted user gets a new booking; seat stays at 0."""
        booker = _make_user(db_session, name="Booker", email="booker@g.test")
        waiter = _make_user(db_session, name="Waiter", email="waiter@g.test")
        f = _make_flight(db_session, economy=1)
        db_session.commit()

        # Booker grabs the seat via direct booking service.
        book_result = booking_service.book_flight(db_session, booker.user_id, booker.name, f.flight_id)
        assert isinstance(book_result, WaitlistOut.__mro__[0]) or hasattr(book_result, 'booking_id')

        # Waiter joins the waitlist (seat is now 0).
        w_result = waitlist_service.join_waitlist(db_session, waiter.user_id, waiter.name, f.flight_id, "economy")
        assert isinstance(w_result, WaitlistOut)

        bookings_before = db_session.query(Booking).filter(Booking.status == "booked").count()

        # Cancel the booker's booking — should trigger promotion.
        booking_obj = db_session.query(Booking).filter(
            Booking.user_id == booker.user_id, Booking.status == "booked"
        ).first()
        cancel_result = booking_service.cancel_booking(db_session, booking_obj.booking_id)
        assert cancel_result.status == "cancelled"

        # Waiter should now have a booking.
        db_session.expire_all()
        bookings_after = db_session.query(Booking).filter(Booking.status == "booked").count()
        assert bookings_after == bookings_before  # one cancelled, one promoted → net same count

        waitlist_entry = db_session.query(Waitlist).filter(Waitlist.waitlist_id == w_result.waitlist_id).first()
        assert waitlist_entry.status == "booked"

        # Seat count must still be 0 (seat freed then immediately re-claimed).
        db_session.refresh(f)
        assert f.economy_seats_available == 0

    def test_cancel_no_waitlist(self, db_session):
        """Cancel with no waiters → seat restored by +1, no new booking."""
        u = _make_user(db_session)
        f = _make_flight(db_session, economy=1)
        db_session.commit()

        booking_service.book_flight(db_session, u.user_id, u.name, f.flight_id)
        booking_obj = db_session.query(Booking).first()
        booking_service.cancel_booking(db_session, booking_obj.booking_id)

        db_session.refresh(f)
        assert f.economy_seats_available == 1

        total_booked = db_session.query(Booking).filter(Booking.status == "booked").count()
        assert total_booked == 0

    def test_cancel_promotes_fifo(self, db_session):
        """Two waiters: the one with the lower waitlist_id (joined first) is promoted."""
        booker = _make_user(db_session, name="Booker", email="booker@g.test")
        waiter1 = _make_user(db_session, name="Waiter1", email="w1@g.test")
        waiter2 = _make_user(db_session, name="Waiter2", email="w2@g.test")
        f = _make_flight(db_session, economy=1)
        db_session.commit()

        booking_service.book_flight(db_session, booker.user_id, booker.name, f.flight_id)

        w1 = waitlist_service.join_waitlist(db_session, waiter1.user_id, waiter1.name, f.flight_id, "economy")
        w2 = waitlist_service.join_waitlist(db_session, waiter2.user_id, waiter2.name, f.flight_id, "economy")
        assert w1.waitlist_id < w2.waitlist_id  # sanity

        booking_obj = db_session.query(Booking).filter(Booking.user_id == booker.user_id).first()
        booking_service.cancel_booking(db_session, booking_obj.booking_id)

        db_session.expire_all()
        e1 = db_session.query(Waitlist).filter(Waitlist.waitlist_id == w1.waitlist_id).first()
        e2 = db_session.query(Waitlist).filter(Waitlist.waitlist_id == w2.waitlist_id).first()

        assert e1.status == "booked"
        assert e2.status == "waiting"

        new_booking = db_session.query(Booking).filter(
            Booking.user_id == waiter1.user_id, Booking.status == "booked"
        ).first()
        assert new_booking is not None

    def test_cancel_skips_stale_promoting_entry(self, db_session):
        """A 'promoting' entry left by a crash is skipped; the next 'waiting' entry is promoted."""
        booker = _make_user(db_session, name="Booker", email="booker@g.test")
        stale_user = _make_user(db_session, name="Stale", email="stale@g.test")
        real_waiter = _make_user(db_session, name="Real", email="real@g.test")
        f = _make_flight(db_session, economy=1)
        db_session.commit()

        booking_service.book_flight(db_session, booker.user_id, booker.name, f.flight_id)

        # Manually inject a 'promoting' entry (simulates a crash mid-promotion).
        stale_entry = _make_waitlist_entry(db_session, stale_user, f, status='promoting')
        real_entry = _make_waitlist_entry(db_session, real_waiter, f, status='waiting')
        db_session.commit()

        # attempt_promotion() should not re-claim the 'promoting' one (its rowcount
        # claim will miss it), but will claim 'waiting' (real_waiter).
        booking_obj = db_session.query(Booking).filter(Booking.user_id == booker.user_id).first()
        booking_service.cancel_booking(db_session, booking_obj.booking_id)

        db_session.expire_all()
        real = db_session.query(Waitlist).filter(Waitlist.waitlist_id == real_entry.waitlist_id).first()
        stale = db_session.query(Waitlist).filter(Waitlist.waitlist_id == stale_entry.waitlist_id).first()

        assert real.status == "booked"
        # Stale entry stays 'promoting' — attempt_promotion only claims 'waiting' rows.
        assert stale.status == "promoting"
