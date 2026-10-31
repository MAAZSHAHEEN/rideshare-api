import enum
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, Index, Integer, PrimaryKeyConstraint, SmallInteger, String, DateTime, Enum as SAEnum, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import JSONB
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
    __table_args__ = (
        # Case-insensitive email identity; created by f6b2d8e0c413 (AST-005).
        Index('uq_users_email_lower', func.lower(text('email')), unique=True),
    )

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
        # Owner listing (GET /rides/me) and FK lookups; created by e3a7c41b9f52.
        Index('ix_rides_driver_id_departure_time_id', 'driver_id', 'departure_time', 'id'),
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
        # Passenger and per-ride booking lists, booking locks and FK lookups;
        # created by e3a7c41b9f52.
        Index('ix_bookings_passenger_id_id', 'passenger_id', 'id'),
        Index('ix_bookings_ride_id_id', 'ride_id', 'id'),
    )

    id         = Column(Integer, primary_key=True)
    ride_id    = Column(Integer, ForeignKey("rides.id"), nullable=False)
    passenger_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status     = Column(SAEnum(BookingStatus), nullable=False, default=BookingStatus.pending)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class IdempotencyKey(Base):
    """Stored outcome of a write sent with an Idempotency-Key header.

    Created by c1a7e0b00001. One row per (user, key); see idempotency.py and
    docs/architecture/BOOKING_SCHEMA_CHANGES.md. ``status_code`` and
    ``response_body`` are only NULL inside the claiming transaction: the
    response is written before that transaction commits.
    """
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        PrimaryKeyConstraint('user_id', 'key', name='pk_idempotency_keys'),
        CheckConstraint("char_length(key::text) >= 1", name='ck_idempotency_keys_key_length'),
        Index('ix_idempotency_keys_expires_at', 'expires_at'),
    )

    user_id       = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key           = Column(String(64), nullable=False)
    operation     = Column(String(32), nullable=False)
    request_hash  = Column(String(64), nullable=False)
    status_code   = Column(SmallInteger, nullable=True)
    response_body = Column(JSONB, nullable=True)
    created_at    = Column(DateTime(timezone=True), nullable=False)
    expires_at    = Column(DateTime(timezone=True), nullable=False)
