from pydantic import AwareDatetime, BaseModel, EmailStr, Field, field_validator
from datetime import datetime
from typing import Literal
from models import UserRole, RideStatus, BookingStatus


PASSWORD_MAX_BYTES = 72


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


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id:    int
    name:  str
    email: str
    role:  UserRole


class UserLogin(BaseModel):
    email:    EmailStr
    # Existing shorter passwords remain usable; new-password policy is above.
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_BYTES, repr=False)

    _validate_password = field_validator("password")(validate_password_input)


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
