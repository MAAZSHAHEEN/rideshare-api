from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import BookingStatus, RideStatus, User
from schemas import DriverBookingResponse, RideCreate, RideResponse
from routers.dependencies import get_current_user
from services import rides as ride_service
from services import bookings as booking_service

router = APIRouter(prefix="/rides", tags=["Rides"])


@router.get("/me", response_model=list[RideResponse])
async def list_my_rides(
    status: RideStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await ride_service.list_my_rides(
        db, current_user, status=status, limit=limit, offset=offset,
    )


@router.get("/{ride_id}", response_model=RideResponse)
async def get_ride(
    ride_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await ride_service.get_ride(ride_id, db)


@router.get("/{ride_id}/bookings", response_model=list[DriverBookingResponse])
async def list_ride_bookings(
    ride_id: int,
    status: BookingStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await booking_service.list_ride_bookings(
        ride_id, db, current_user, status=status, limit=limit, offset=offset,
    )


@router.patch("/{ride_id}/complete", response_model=RideResponse)
async def complete_ride(
    ride_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await ride_service.complete_ride(
        ride_id=ride_id,
        db=db,
        current_user=current_user,
    )


@router.patch("/{ride_id}/cancel", response_model=RideResponse)
async def cancel_ride(
    ride_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await ride_service.cancel_ride(
        ride_id=ride_id,
        db=db,
        current_user=current_user,
    )


@router.post("/", response_model=RideResponse, status_code=201)
async def create_ride(
    ride_data: RideCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return await ride_service.create_ride(
        ride_data=ride_data,
        db=db,
        current_user=current_user,
    )


@router.get("/", response_model=list[RideResponse])
async def search_rides(
    origin: Annotated[str | None, Query(min_length=1)] = None,
    destination: Annotated[str | None, Query(min_length=1)] = None,
    departure_from: Annotated[AwareDatetime | None, Query()] = None,
    departure_before: Annotated[AwareDatetime | None, Query()] = None,
    min_seats: Annotated[int, Query(ge=1, le=2**31 - 1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return await ride_service.search_rides(
        origin=origin,
        destination=destination,
        departure_from=departure_from,
        departure_before=departure_before,
        min_seats=min_seats,
        limit=limit,
        offset=offset,
        db=db,
    )
