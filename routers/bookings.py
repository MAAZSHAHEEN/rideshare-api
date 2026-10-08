from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User
from routers.dependencies import get_current_user
from schemas import BookingResponse
from services import bookings as booking_service

router = APIRouter(prefix="/bookings", tags=["Bookings"])


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
