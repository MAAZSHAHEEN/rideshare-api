from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import BookingStatus, User
from routers.dependencies import get_current_user
from schemas import BookingResponse, PassengerBookingResponse
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await booking_service.book_ride(ride_id=ride_id, db=db, current_user=current_user)


@router.patch("/{booking_id}/respond", response_model=BookingResponse)
async def respond_to_booking(
    booking_id: int,
    accept: bool,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await booking_service.respond_to_booking(booking_id=booking_id, accept=accept, db=db, current_user=current_user)
