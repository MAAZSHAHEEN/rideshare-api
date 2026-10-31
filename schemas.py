from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, StrictInt, computed_field, field_validator
from datetime import datetime
from typing import Literal
from models import UserRole, RideStatus, BookingStatus
from permissions import AccountMode, allowed_modes


PASSWORD_MAX_BYTES = 72


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def normalize_email(value: str) -> str:
    """Trim and lowercase ASCII letters only.

    Case-insensitive identity itself is PostgreSQL lower(), used by both the login
    lookup and uq_users_email_lower. Python's str.lower() applies full Unicode case
    mapping and can disagree with PostgreSQL (e.g. U+0130 'İ' becomes 'i' + U+0307
    in Python but 'i' in PostgreSQL), which would lock out legacy accounts and let
    case twins register. ASCII-only folding is identical in both, so it is safe.
    """
    return value.strip().translate(_ASCII_LOWER)


def normalize_email_input(value):
    # Non-strings fall through to EmailStr's own validation error.
    return normalize_email(value) if isinstance(value, str) else value


def validate_password_input(value: str) -> str:
    if not value.strip():
        raise ValueError("Password must not be blank")
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError("Password must be at most 72 UTF-8 bytes")
    return value  # Preserve spaces and Unicode exactly; never truncate or normalize.


class UserCreate(BaseModel):
    name:         str
    email:        EmailStr
    password:     str = Field(min_length=12, max_length=PASSWORD_MAX_BYTES, repr=False)
    cnic:         str
    phone_number: str
    role:         Literal[UserRole.passenger, UserRole.driver] = UserRole.passenger

    _validate_password = field_validator("password")(validate_password_input)
    _normalize_email = field_validator("email", mode="before")(normalize_email_input)


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id:    int
    name:  str
    email: str
    role:  UserRole

    @computed_field
    @property
    def allowed_modes(self) -> list[AccountMode]:
        return allowed_modes(self.role)


class UserLogin(BaseModel):
    email:    EmailStr
    # Existing shorter passwords remain usable; new-password policy is above.
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_BYTES, repr=False)

    _validate_password = field_validator("password")(validate_password_input)
    _normalize_email = field_validator("email", mode="before")(normalize_email_input)


class Token(BaseModel):
    access_token: str
    token_type:   str = "bearer"


class RideCreate(BaseModel):
    origin:          str
    destination:     str
    departure_time:  AwareDatetime
    available_seats: int = Field(ge=0)
    fare_per_seat:   int = Field(ge=0)


class RideResponse(BaseModel):
    model_config = {"from_attributes": True}

    id:              int
    driver_id:       int
    origin:          str
    destination:     str
    departure_time:  datetime
    available_seats: int
    fare_per_seat:   int
    status:          RideStatus


# Hard bound per booking; equals the DB CHECK ck_bookings_seats_requested_range
# (c1a7e0b00003). BOOKING_MAX_SEATS (lifecycle.py) may lower it at runtime.
MAX_SEATS_PER_BOOKING = 8


class BookingCreate(BaseModel):
    """Optional body of POST /bookings/{ride_id}. Absent body or field = 1 seat.

    StrictInt: "2", 2.0 and true are rejected instead of coerced.
    """
    model_config = ConfigDict(extra="forbid")

    seats: StrictInt = Field(default=1, ge=1, le=MAX_SEATS_PER_BOOKING)


class BookingResponse(BaseModel):
    model_config = {"from_attributes": True}

    id:              int
    ride_id:         int
    passenger_id:    int
    status:          BookingStatus
    # Additive (c1a7e0b00003): seats requested, and held once accepted.
    seats_requested: int


class PassengerSummary(BaseModel):
    id: int
    name: str


class PassengerBookingResponse(BookingResponse):
    ride: RideResponse


class DriverBookingResponse(BookingResponse):
    passenger: PassengerSummary


class RideBookingCounts(BaseModel):
    """Booking counts by status for one of the driver's rides (D2)."""
    ride_id:   int
    pending:   int
    accepted:  int
    rejected:  int
    cancelled: int
