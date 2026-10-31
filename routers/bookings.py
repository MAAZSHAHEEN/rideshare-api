from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

import idempotency
from database import get_db
from models import BookingStatus, User
from routers.dependencies import get_current_user
from schemas import BookingCreate, BookingResponse, PassengerBookingResponse
from services import bookings as booking_service

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.get("/me", response_model=list[PassengerBookingResponse])
async def list_my_bookings(
    status: BookingStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await booking_service.list_my_bookings(
        db, current_user, status=status, limit=limit, offset=offset,
    )


@router.patch("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await booking_service.cancel_booking(booking_id=booking_id, db=db, current_user=current_user)


@router.post("/{ride_id}", response_model=BookingResponse, status_code=201)
async def book_ride(
    ride_id: int,
    body: Annotated[BookingCreate | None, Body()] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Depends(idempotency.key_header),
):
    # No body (the pre-c1a7e0b00003 contract), null or {} all mean 1 seat.
    seats = body.seats if body is not None else 1
    if idempotency_key is None:
        return await booking_service.book_ride(
            ride_id=ride_id, db=db, current_user=current_user, seats=seats)
    # ``seats`` is part of the fingerprint, so one key cannot be replayed for a
    # different seat count (422). It is omitted when 1, keeping fingerprints
    # of keys stored before this change valid for the same 1-seat request.
    parameters = {"ride_id": ride_id} if seats == 1 else {"ride_id": ride_id, "seats": seats}
    claim = await idempotency.claim(db, current_user.id, idempotency_key,
                                    "booking.create", parameters)
    if claim.replay is not None:
        return claim.replay
    return await booking_service.book_ride(
        ride_id=ride_id, db=db, current_user=current_user, seats=seats,
        before_commit=claim.recorder(201, BookingResponse),
    )


@router.patch("/{booking_id}/respond", response_model=BookingResponse)
async def respond_to_booking(
    booking_id: int,
    accept: bool,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Depends(idempotency.key_header),
):
    if idempotency_key is None:
        return await booking_service.respond_to_booking(
            booking_id=booking_id, accept=accept, db=db, current_user=current_user)
    claim = await idempotency.claim(db, current_user.id, idempotency_key, "booking.respond",
                                    {"booking_id": booking_id, "accept": accept})
    if claim.replay is not None:
        return claim.replay
    return await booking_service.respond_to_booking(
        booking_id=booking_id, accept=accept, db=db, current_user=current_user,
        before_commit=claim.recorder(200, BookingResponse),
    )
