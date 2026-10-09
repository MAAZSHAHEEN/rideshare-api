"""Ride operations using the caller's request-scoped AsyncSession.

Writes commit once and refresh the returned Ride. Cancellation and completion
explicitly roll back failures inside their protected operation. Creation rolls
back pre-commit exceptions. Refresh remains outside rollback handlers because
its failure cannot undo an earlier commit. Search only reads; the caller owns
session closure, including cancellation cleanup. No hidden/nested transactions
are created here. HTTPException preserves the existing API contract.

Commit includes unrelated pending changes and rollback discards them. Supply a
clean session: these operations are not independently composable transactions.
"""

from collections.abc import Sequence
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Booking, BookingStatus, Ride, RideStatus, User, UserRole
from schemas import RideCreate


async def get_ride(ride_id: int, db: AsyncSession) -> Ride:
    ride = await db.scalar(select(Ride).where(Ride.id == ride_id))
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    return ride


async def list_my_rides(
    db: AsyncSession, current_user: User, *,
    status: RideStatus | None = None, limit: int = 20, offset: int = 0,
) -> Sequence[Ride]:
    if current_user.role != UserRole.driver:
        raise HTTPException(status_code=403, detail="Only drivers can list their rides")
    query = select(Ride).where(Ride.driver_id == current_user.id)
    if status is not None:
        query = query.where(Ride.status == status)
    return (await db.scalars(query.order_by(
        Ride.departure_time.desc(), Ride.id.desc(),
    ).limit(limit).offset(offset))).all()


async def complete_ride(
    ride_id: int,
    db: AsyncSession,
    current_user: User,
) -> Ride:
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


async def cancel_ride(
    ride_id: int,
    db: AsyncSession,
    current_user: User,
) -> Ride:
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


async def create_ride(
    ride_data: RideCreate,
    db: AsyncSession,
    current_user: User
) -> Ride:
    try:
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
    except Exception:
        await db.rollback()
        raise

    await db.refresh(new_ride)
    return new_ride


async def search_rides(
    db: AsyncSession,
    *,
    origin: str | None = None,
    destination: str | None = None,
    departure_from: datetime | None = None,
    departure_before: datetime | None = None,
    min_seats: int = 1,
    limit: int = 20,
    offset: int = 0,
) -> Sequence[Ride]:
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
