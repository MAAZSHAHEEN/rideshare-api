from pydantic import BaseModel, EmailStr
from datetime import datetime
from models import UserRole, RideStatus, BookingStatus


class UserCreate(BaseModel):
    name:         str
    email:        EmailStr
    password:     str
    cnic:         str
    phone_number: str
    role:         UserRole = UserRole.passenger


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id:    int
    name:  str
    email: str
    role:  UserRole


class UserLogin(BaseModel):
    email:    EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type:   str = "bearer"


class RideCreate(BaseModel):
    origin:          str
    destination:     str
    departure_time:  datetime
    available_seats: int
    fare_per_seat:   int


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