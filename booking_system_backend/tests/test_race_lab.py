"""Tests for the Race Lab demo endpoints.

Covers:
- POST /demo/race-lab/reset is idempotent
- reset only affects the demo flight (other flights untouched)
- demo flight is excluded from GET /flights
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Booking, Flight, User
from services.race_lab import DEMO_FLIGHT_ORIGIN


class TestRaceLabReset:
    """Tests for POST /demo/race-lab/reset."""

    def test_reset_returns_flight_id_and_20_users(self, client):
        """Reset returns a flight_id and exactly 20 racer users."""
        response = client.post("/demo/race-lab/reset")
        assert response.status_code == 200
        data = response.json()
        assert "flight_id" in data
        assert isinstance(data["flight_id"], int)
        assert len(data["users"]) == 20

    def test_reset_users_are_named_racer01_to_racer20(self, client):
        """Users returned are racer01..racer20."""
        response = client.post("/demo/race-lab/reset")
        names = [u["name"] for u in response.json()["users"]]
        expected = [f"racer{i:02d}" for i in range(1, 21)]
        assert names == expected

    def test_reset_is_idempotent(self, client):
        """Calling reset twice returns the same flight_id and users."""
        r1 = client.post("/demo/race-lab/reset").json()
        r2 = client.post("/demo/race-lab/reset").json()
        assert r1["flight_id"] == r2["flight_id"]
        assert [u["user_id"] for u in r1["users"]] == [u["user_id"] for u in r2["users"]]

    def test_reset_restores_5_economy_seats(self, client, db_session):
        """After booking, reset restores seat count to exactly 5."""
        # First reset
        r = client.post("/demo/race-lab/reset").json()
        flight_id = r["flight_id"]
        user_id = r["users"][0]["user_id"]
        name = r["users"][0]["name"]

        # Book one seat to deplete count by 1
        book_resp = client.post("/book", json={
            "user_id": user_id, "name": name,
            "flight_id": flight_id, "seat_class": "economy"
        })
        assert book_resp.status_code == 200

        # Reset again — seats should be back to 5
        client.post("/demo/race-lab/reset")
        flight = db_session.query(Flight).filter(
            Flight.flight_id == flight_id
        ).first()
        assert flight.economy_seats_available == 5

    def test_reset_deletes_previous_bookings_on_demo_flight(self, client, db_session):
        """Reset deletes existing bookings for the demo flight."""
        r = client.post("/demo/race-lab/reset").json()
        flight_id = r["flight_id"]
        user_id = r["users"][0]["user_id"]
        name = r["users"][0]["name"]

        # Book a seat
        client.post("/book", json={
            "user_id": user_id, "name": name,
            "flight_id": flight_id, "seat_class": "economy"
        })
        bookings_before = db_session.query(Booking).filter(
            Booking.flight_id == flight_id
        ).count()
        assert bookings_before == 1

        # Reset clears them
        client.post("/demo/race-lab/reset")
        db_session.expire_all()
        bookings_after = db_session.query(Booking).filter(
            Booking.flight_id == flight_id
        ).count()
        assert bookings_after == 0

    def test_reset_does_not_touch_other_flights(self, client, db_session):
        """Reset must not modify bookings on non-demo flights."""
        # Create a normal flight and a user
        normal_flight = Flight(
            origin="Earth", destination="Mars",
            departure_time="2099-01-01 09:00", arrival_time="2099-01-01 17:00",
            base_price=1000,
            economy_seats_available=10, business_seats_available=5,
            galaxium_seats_available=2
        )
        db_session.add(normal_flight)
        normal_user = User(name="Alice", email="alice@example.com")
        db_session.add(normal_user)
        db_session.commit()

        # Book on the normal flight
        resp = client.post("/book", json={
            "user_id": normal_user.user_id,
            "name": "Alice",
            "flight_id": normal_flight.flight_id,
            "seat_class": "economy"
        })
        assert resp.status_code == 200

        # Reset race lab
        client.post("/demo/race-lab/reset")
        db_session.expire_all()

        # The normal booking must still exist
        surviving = db_session.query(Booking).filter(
            Booking.flight_id == normal_flight.flight_id
        ).count()
        assert surviving == 1

    def test_reset_does_not_touch_other_users(self, client, db_session):
        """Reset must not delete or modify non-racer users."""
        # Create a normal user
        normal_user = User(name="Bob", email="bob@example.com")
        db_session.add(normal_user)
        db_session.commit()
        uid = normal_user.user_id

        client.post("/demo/race-lab/reset")
        db_session.expire_all()

        still_there = db_session.query(User).filter(User.user_id == uid).first()
        assert still_there is not None
        assert still_there.name == "Bob"


class TestDemoFlightHiddenFromFlightsEndpoint:
    """The demo flight must never appear in GET /flights."""

    def test_demo_flight_not_in_flights_list_before_reset(self, client, db_session):
        """Even if the demo flight somehow exists, it is excluded from /flights."""
        db_session.add(Flight(
            origin=DEMO_FLIGHT_ORIGIN,
            destination="Race Lab Station",
            departure_time="2099-12-31 00:00", arrival_time="2099-12-31 12:00",
            base_price=1,
            economy_seats_available=5, business_seats_available=0,
            galaxium_seats_available=0
        ))
        db_session.commit()
        response = client.get("/flights")
        assert response.status_code == 200
        origins = [f["origin"] for f in response.json()]
        assert DEMO_FLIGHT_ORIGIN not in origins

    def test_demo_flight_not_in_flights_list_after_reset(self, client):
        """After reset, /flights still does not include the demo flight."""
        client.post("/demo/race-lab/reset")
        response = client.get("/flights")
        assert response.status_code == 200
        origins = [f["origin"] for f in response.json()]
        assert DEMO_FLIGHT_ORIGIN not in origins

    def test_normal_flights_still_returned_after_reset(self, client, db_session):
        """Normal flights are unaffected by reset."""
        db_session.add(Flight(
            origin="Earth", destination="Venus",
            departure_time="2099-06-01 10:00", arrival_time="2099-06-01 18:00",
            base_price=500,
            economy_seats_available=8, business_seats_available=4,
            galaxium_seats_available=1
        ))
        db_session.commit()

        client.post("/demo/race-lab/reset")
        response = client.get("/flights")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["destination"] == "Venus"
