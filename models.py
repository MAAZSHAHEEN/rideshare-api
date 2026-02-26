import enum
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, Enum as SAEnum, ForeignKey
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

    id           = Column(Integer, primary_key=True, index=True)
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

    id              = Column(Integer, primary_key=True, index=True)
    driver_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    origin          = Column(String, nullable=False)
    destination     = Column(String, nullable=False)
    departure_time  = Column(DateTime(timezone=True), nullable=False)
    available_seats = Column(Integer, nullable=False)
    fare_per_seat   = Column(Integer, nullable=False)
    status          = Column(SAEnum(RideStatus), default=RideStatus.active)
    created_at      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class BookingStatus(str, enum.Enum):
    pending   = "pending"
    accepted  = "accepted"
    rejected  = "rejected"
    cancelled = "cancelled"


class Booking(Base):
    __tablename__ = "bookings"

    id         = Column(Integer, primary_key=True, index=True)
    ride_id    = Column(Integer, ForeignKey("rides.id"), nullable=False)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status     = Column(SAEnum(BookingStatus), default=BookingStatus.pending)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))