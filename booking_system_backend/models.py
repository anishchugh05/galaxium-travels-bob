from enum import Enum

from sqlalchemy import Column, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class BookingStatus(str, Enum):
    BOOKED = "booked"
    CANCELLED = "cancelled"
    CANCELED = "cancelled"  # American spelling alias  # noqa: PIE796
    COMPLETED = "completed"

class User(Base):
    __tablename__ = 'users'
    user_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)

class Flight(Base):
    __tablename__ = 'flights'
    flight_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    origin = Column(String, nullable=False)
    destination = Column(String, nullable=False)
    departure_time = Column(String, nullable=False)
    arrival_time = Column(String, nullable=False)
    base_price = Column(Integer, nullable=False)  # Economy price (1x)
    economy_seats_available = Column(Integer, nullable=False)  # 60% of total
    business_seats_available = Column(Integer, nullable=False)  # 30% of total
    galaxium_seats_available = Column(Integer, nullable=False)  # 10% of total

class Booking(Base):
    __tablename__ = 'bookings'
    booking_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    flight_id = Column(Integer, ForeignKey('flights.flight_id'), nullable=False)
    status = Column(String, nullable=False)
    booking_time = Column(String, nullable=False)
    seat_class = Column(String, nullable=False, default='economy')  # economy/business/galaxium
    price_paid = Column(Integer, nullable=False)  # Actual price at booking time


class Waitlist(Base):
    __tablename__ = 'waitlist'
    waitlist_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    flight_id = Column(Integer, ForeignKey('flights.flight_id'), nullable=False)
    seat_class = Column(String, nullable=False)
    # Lifecycle: waiting → promoting → booked (success)
    #            waiting → promoting → waiting (seat gone in race)
    #            waiting → promoting → failed  (other error; queue advances)
    #            waiting → cancelled            (user left voluntarily)
    status = Column(String, nullable=False, default='waiting')
    added_at = Column(String, nullable=False)  # ISO datetime, same pattern as booking_time

    # Partial unique index: only one 'waiting' entry allowed per user/flight/class.
    # Entries in other statuses (booked, cancelled, failed) don't count — user can rejoin.
    __table_args__ = (
        Index(
            'uq_waitlist_active',
            'user_id', 'flight_id', 'seat_class',
            unique=True,
            sqlite_where=text("status = 'waiting'"),
            postgresql_where=text("status = 'waiting'"),
        ),
    )