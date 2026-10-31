import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, Column, DDL, Index, Integer, String, DateTime, Enum as SAEnum, ForeignKey, event, func, text
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


class AccountStatus(str, enum.Enum):
    active    = "active"
    suspended = "suspended"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Case-insensitive email identity; created by f6b2d8e0c413 (AST-005).
        Index('uq_users_email_lower', func.lower(text('email')), unique=True),
        # VARCHAR + CHECK, not a PostgreSQL enum: adding a state later is a
        # constraint swap, not ALTER TYPE ... ADD VALUE. Created by d5e1a0b00001.
        CheckConstraint(
            "account_status IN ('active', 'suspended')", name='ck_users_account_status',
        ),
        CheckConstraint('token_version >= 0', name='ck_users_token_version_nonnegative'),
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
    # Milestone D (d5e1a0b00001). Server defaults fill legacy rows; every
    # suspension and reinstatement increments token_version, which revokes all
    # previously issued access tokens (their `tv` claim no longer matches).
    account_status = Column(
        String(16), nullable=False, default=AccountStatus.active.value,
        server_default=text("'active'"),
    )
    token_version = Column(Integer, nullable=False, default=0, server_default=text("0"))


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


# Append-only enforcement for admin_audit_log. Migration d5e1a0b00002 creates
# the same objects (frozen copy); this listener gives schemas built with
# metadata.create_all (tests) identical behaviour.
AUDIT_LOG_IMMUTABLE_FUNCTION_SQL = """
CREATE FUNCTION admin_audit_log_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'admin_audit_log is append-only (% blocked)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$
"""
AUDIT_LOG_ROW_TRIGGER_SQL = (
    "CREATE TRIGGER admin_audit_log_no_update_delete BEFORE UPDATE OR DELETE "
    "ON admin_audit_log FOR EACH ROW EXECUTE FUNCTION admin_audit_log_immutable()"
)
AUDIT_LOG_TRUNCATE_TRIGGER_SQL = (
    "CREATE TRIGGER admin_audit_log_no_truncate BEFORE TRUNCATE "
    "ON admin_audit_log FOR EACH STATEMENT EXECUTE FUNCTION admin_audit_log_immutable()"
)


class AdminAuditLog(Base):
    """One row per admin action, written in the same transaction as the action.

    Never store PII here: no CNIC, phone, email or name. `details` (column
    "metadata"; the attribute name avoids SQLAlchemy's reserved `metadata`)
    holds ids, states and counts only, validated by audit.record().
    """
    __tablename__ = "admin_audit_log"
    __table_args__ = (
        Index('ix_admin_audit_log_target', 'target_type', 'target_id', 'id'),
        Index('ix_admin_audit_log_actor_id_id', 'actor_id', 'id'),
    )

    id          = Column(BigInteger, primary_key=True)
    # NULL is reserved for operator scripts and system jobs.
    actor_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    action      = Column(String(64), nullable=False)
    target_type = Column(String(32), nullable=False)
    target_id   = Column(BigInteger, nullable=False)
    reason      = Column(String(500), nullable=True)
    details     = Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


for _statement in (AUDIT_LOG_IMMUTABLE_FUNCTION_SQL, AUDIT_LOG_ROW_TRIGGER_SQL,
                   AUDIT_LOG_TRUNCATE_TRIGGER_SQL):
    event.listen(AdminAuditLog.__table__, "after_create",
                 DDL(_statement.replace("%", "%%")).execute_if(dialect="postgresql"))
