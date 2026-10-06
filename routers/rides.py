from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import Booking, BookingStatus, Ride, RideStatus, User, UserRole
from schemas import RideCreate, RideResponse
from routers.dependencies import get_current_user

router = APIRouter(prefix="/rides", tags=["Rides"])


@router.patch("/{ride_id}/complete", response_model=RideResponse)
async def complete_ride(
    ride_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.driver:
        raise HTTPException(status_code=403, detail="Only drivers can complete rides")

    try:
        ride = await db.scalar(
            select(Ride).where(Ride.id == ride_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if ride is None:
            raise HTTPException(status_code=404, detail="Ride not found")
        if ride.driver_id != current_user.id:
            raise HTTPException(status_code=403, detail="This is not your ride")
        if ride.status != RideStatus.active:
            raise HTTPException(status_code=409, detail="Ride is not active")

        # The Ride lock serializes all booking writers. Only pending bookings
        # change on completion; accepted bookings retain their consumed seats.
        pending = (await db.scalars(
            select(Booking).where(
                Booking.ride_id == ride_id, Booking.status == BookingStatus.pending,
            ).order_by(Booking.id.asc()).with_for_update()
            .execution_options(populate_existing=True)
        )).all()
        for booking in pending:
            booking.status = BookingStatus.cancelled
        ride.status = RideStatus.completed
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(ride)
    return ride


@router.patch("/{ride_id}/cancel", response_model=RideResponse)
async def cancel_ride(
    ride_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.driver:
        raise HTTPException(status_code=403, detail="Only drivers can cancel rides")

    try:
        ride = await db.scalar(
            select(Ride).where(Ride.id == ride_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if ride is None:
            raise HTTPException(status_code=404, detail="Ride not found")
        if ride.driver_id != current_user.id:
            raise HTTPException(status_code=403, detail="This is not your ride")
        if ride.status != RideStatus.active:
            raise HTTPException(status_code=409, detail="Ride is not active")

        # All booking writers lock Ride first. Holding it prevents new bookings
        # or decisions while we lock the live bookings in a consistent order.
        bookings = (await db.scalars(
            select(Booking).where(
                Booking.ride_id == ride_id,
                Booking.status.in_([BookingStatus.pending, BookingStatus.accepted]),
            ).order_by(Booking.id.asc()).with_for_update()
            .execution_options(populate_existing=True)
        )).all()
        for booking in bookings:
            if booking.status == BookingStatus.accepted:
                # Reverse a known debit, without inferring an original capacity.
                ride.available_seats += 1
            booking.status = BookingStatus.cancelled
        ride.status = RideStatus.cancelled
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(ride)
    return ride


@router.post("/", response_model=RideResponse, status_code=201)
async def create_ride(
    ride_data: RideCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Only drivers can create rides
    if current_user.role != UserRole.driver:
        raise HTTPException(status_code=403, detail="Only drivers can create rides")

    departure_time = ride_data.departure_time.astimezone(timezone.utc)
    if departure_time <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="Departure time must be in the future")

    new_ride = Ride(
        driver_id=current_user.id,
        origin=ride_data.origin,
        destination=ride_data.destination,
        departure_time=departure_time,
        available_seats=ride_data.available_seats,
        fare_per_seat=ride_data.fare_per_seat,
    )
    db.add(new_ride)
    await db.commit()
    await db.refresh(new_ride)
    return new_ride


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
    if departure_from is not None:
        departure_from = departure_from.astimezone(timezone.utc)
    if departure_before is not None:
        departure_before = departure_before.astimezone(timezone.utc)
    if (departure_from is not None and departure_before is not None
            and departure_from >= departure_before):
        raise HTTPException(status_code=422, detail="departure_from must be before departure_before")

    query = select(Ride).where(
        Ride.status == RideStatus.active,
        Ride.available_seats >= min_seats,
        Ride.departure_time > datetime.now(timezone.utc),
    )
    if origin is not None:
        query = query.where(Ride.origin == origin)
    if destination is not None:
        query = query.where(Ride.destination == destination)
    if departure_from is not None:
        query = query.where(Ride.departure_time >= departure_from)
    if departure_before is not None:
        query = query.where(Ride.departure_time < departure_before)

    result = await db.execute(
        query.order_by(Ride.departure_time.asc(), Ride.id.asc()).limit(limit).offset(offset)
    )
    return result.scalars().all()
