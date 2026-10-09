import enum
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, Index, Integer, String, DateTime, Enum as SAEnum, ForeignKey, text
from database import Base


class UserRole(str, enum.Enum):
    passenger = "passenger"
    driver    = "driver"
    admin     = "admin"


class RideStatus(str, enum.Enum):
    active    = "active"
    completed = "completed"
    cancelled = "cancelled"


class User(Base):
    __tablename__ = "users"

    id           = Column(Integer, primary_key=True)
    name         = Column(String, nullable=False)
    email        = Column(String, unique=True, index=True, nullable=False)
    password     = Column(String, nullable=False)
    cnic         = Column(String, unique=True, nullable=False)
    phone_number = Column(String, nullable=False)
    role         = Column(SAEnum(UserRole), nullable=False, default=UserRole.passenger)
    created_at   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Ride(Base):
    __tablename__ = "rides"
    __table_args__ = (
        Index('ix_rides_status_departure_time_id', 'status', 'departure_time', 'id'),
        CheckConstraint('available_seats >= 0', name='ck_rides_available_seats_nonnegative'),
        CheckConstraint('fare_per_seat >= 0', name='ck_rides_fare_per_seat_nonnegative'),
    )

    id              = Column(Integer, primary_key=True)
    driver_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    origin          = Column(String, nullable=False)
    destination     = Column(String, nullable=False)
    departure_time  = Column(DateTime(timezone=True), nullable=False)
    available_seats = Column(Integer, nullable=False)
    fare_per_seat   = Column(Integer, nullable=False)
    status          = Column(SAEnum(RideStatus), nullable=False, default=RideStatus.active)
    created_at      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class BookingStatus(str, enum.Enum):
    pending   = "pending"
    accepted  = "accepted"
    rejected  = "rejected"
    cancelled = "cancelled"


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        Index(
            'uq_bookings_active_passenger_ride', 'ride_id', 'passenger_id',
            unique=True, postgresql_where=text("status IN ('pending', 'accepted')"),
        ),
    )

    id         = Column(Integer, primary_key=True)
    ride_id    = Column(Integer, ForeignKey("rides.id"), nullable=False)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status     = Column(SAEnum(BookingStatus), nullable=False, default=BookingStatus.pending)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
