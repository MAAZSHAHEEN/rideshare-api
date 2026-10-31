import re
from pydantic import AwareDatetime, BaseModel, EmailStr, Field, computed_field, field_validator, model_validator
from datetime import datetime
from typing import Literal
from models import UserRole, RideStatus, BookingStatus
from permissions import AccountMode, allowed_modes


PASSWORD_MAX_BYTES = 72
NAME_MAX_LENGTH = 100
PLACE_MAX_LENGTH = 100
CNIC_INPUT_MAX_LENGTH = 32
PHONE_INPUT_MAX_LENGTH = 32
RIDE_SEATS_MIN = 1
RIDE_SEATS_MAX = 8
FARE_PER_SEAT_MAX = 100_000  # PKR; far above any intercity seat fare, well inside int4.

# These messages are allowlisted in validation_errors.py. They are fixed text and
# must never interpolate the submitted value.
CNIC_ERROR = "CNIC must be 13 digits, as XXXXX-XXXXXXX-X or XXXXXXXXXXXXX"
PHONE_ERROR = "Phone number must be a Pakistani mobile number, as 03XXXXXXXXX or +923XXXXXXXXX"
BLANK_ERROR = "Value must not be blank"
CONTROL_CHAR_ERROR = "Value must not contain control characters"
SAME_PLACE_ERROR = "Origin and destination must be different"

# ASCII digits only: \d would also accept Arabic-Indic and other Unicode digits.
_CNIC_PATTERN = re.compile(r"[0-9]{13}|[0-9]{5}-[0-9]{7}-[0-9]")
_PHONE_PATTERN = re.compile(r"(?:0|\+92|92)(3[0-9]{9})")
_PHONE_SEPARATORS = re.compile(r"[ -]")
# C0/C1 controls, plus invisible characters that spoof how a name or place is
# displayed to other users: bidi embeddings/overrides/isolates (U+202A-202E,
# U+2066-2069, the "Trojan Source" set), zero-width space U+200B, word joiner
# U+2060, invisible operators U+2061-2064, BOM U+FEFF and Mongolian vowel
# separator U+180E. ZWNJ/ZWJ (U+200C/U+200D) and LRM/RLM (U+200E/U+200F) stay
# allowed: Urdu and other scripts use them in ordinary names.
_CONTROL_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f\u180e\u200b\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def normalize_cnic(value: str) -> str:
    """Canonical stored form for NEW input: 13 ASCII digits, no hyphens."""
    value = value.strip()
    if not _CNIC_PATTERN.fullmatch(value):
        raise ValueError(CNIC_ERROR)
    return value.replace("-", "")


def normalize_pk_mobile(value: str) -> str:
    """03XXXXXXXXX / +923XXXXXXXXX / 923XXXXXXXXX (spaces or hyphens allowed) -> +923XXXXXXXXX."""
    match = _PHONE_PATTERN.fullmatch(_PHONE_SEPARATORS.sub("", value.strip()))
    if not match:
        raise ValueError(PHONE_ERROR)
    return "+92" + match.group(1)


def clean_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(BLANK_ERROR)
    if _CONTROL_CHARS.search(value):
        raise ValueError(CONTROL_CHAR_ERROR)
    return value


def validate_password_input(value: str) -> str:
    if not value.strip():
        raise ValueError("Password must not be blank")
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError("Password must be at most 72 UTF-8 bytes")
    return value  # Preserve spaces and Unicode exactly; never truncate or normalize.


class UserCreate(BaseModel):
    name:         str = Field(max_length=NAME_MAX_LENGTH)
    email:        EmailStr
    password:     str = Field(min_length=12, max_length=PASSWORD_MAX_BYTES, repr=False)
    cnic:         str = Field(max_length=CNIC_INPUT_MAX_LENGTH, repr=False)
    phone_number: str = Field(max_length=PHONE_INPUT_MAX_LENGTH, repr=False)
    role:         Literal[UserRole.passenger, UserRole.driver] = UserRole.passenger

    _validate_password = field_validator("password")(validate_password_input)
    _validate_name = field_validator("name")(clean_text)
    _validate_cnic = field_validator("cnic")(normalize_cnic)
    _validate_phone = field_validator("phone_number")(normalize_pk_mobile)


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


class Token(BaseModel):
    access_token: str
    token_type:   str = "bearer"


class RideCreate(BaseModel):
    origin:          str = Field(max_length=PLACE_MAX_LENGTH)
    destination:     str = Field(max_length=PLACE_MAX_LENGTH)
    departure_time:  AwareDatetime
    # Product decision (Maaz, 31 Oct): a NEW ride must offer at least one seat.
    # Input validation only: the DB CHECK stays >= 0, existing rows with 0 seats
    # are untouched, and seats still reach 0 as bookings are accepted.
    available_seats: int = Field(ge=RIDE_SEATS_MIN, le=RIDE_SEATS_MAX)
    fare_per_seat:   int = Field(ge=0, le=FARE_PER_SEAT_MAX)

    _validate_places = field_validator("origin", "destination")(clean_text)

    @model_validator(mode="after")
    def _distinct_places(self):
        if self.origin.casefold() == self.destination.casefold():
            raise ValueError(SAME_PLACE_ERROR)
        return self


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


class BookingResponse(BaseModel):
    model_config = {"from_attributes": True}

    id:           int
    ride_id:      int
    passenger_id: int
    status:       BookingStatus


class PassengerSummary(BaseModel):
    id: int
    name: str


class PassengerBookingResponse(BookingResponse):
    ride: RideResponse


class DriverBookingResponse(BookingResponse):
    passenger: PassengerSummary
