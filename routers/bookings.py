from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Booking, BookingStatus, Ride, RideStatus, User, UserRole
from routers.dependencies import get_current_user
from schemas import BookingResponse

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.patch("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.passenger:
        raise HTTPException(status_code=403, detail="Only passengers can cancel bookings")

    try:
        # Read only the routing key before locking, never lifecycle state.
        ride_id = await db.scalar(select(Booking.ride_id).where(Booking.id == booking_id))
        if ride_id is None:
            raise HTTPException(status_code=404, detail="Booking not found")

        # Match driver responses: Ride -> Booking, refreshing identity-map state
        # after each lock wait. Both changes belong to this session's transaction.
        ride = await db.scalar(
            select(Ride).where(Ride.id == ride_id).with_for_update()
            .execution_options(populate_existing=True)
        )
        if ride is None:
            raise HTTPException(status_code=404, detail="Ride not found")

        booking = await db.scalar(
            select(Booking)
            .where(Booking.id == booking_id, Booking.ride_id == ride_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        if booking.passenger_id != current_user.id:
            raise HTTPException(status_code=403, detail="This is not your booking")
        if booking.status not in (BookingStatus.pending, BookingStatus.accepted):
            raise HTTPException(status_code=409, detail="Booking cannot be cancelled")

        if booking.status == BookingStatus.accepted:
            ride.available_seats += 1
        booking.status = BookingStatus.cancelled
        await db.commit()
    except Exception:
        # Rollback also expires ORM state; failed writes must not look persisted.
        await db.rollback()
        raise

    await db.refresh(booking)
    return booking


@router.post("/{ride_id}", response_model=BookingResponse, status_code=201)
async def book_ride(
    ride_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Only passengers can book
    if current_user.role != UserRole.passenger:
        raise HTTPException(status_code=403, detail="Only passengers can book rides")

    # Lock the ride row for the duration of this transaction to prevent
    # concurrent requests from double-booking the last seat.
    result = await db.execute(
        select(Ride).where(Ride.id == ride_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    ride = result.scalar_one_or_none()

    if not ride:
        raise HTTPException(status_code=404, detail="Ride not found")
    if ride.status != RideStatus.active:
        raise HTTPException(status_code=400, detail="Ride is not active")
    if ride.available_seats < 1:
        raise HTTPException(status_code=400, detail="No seats available")

    # Check if passenger already booked this ride
    result = await db.execute(
        select(Booking).where(
            Booking.ride_id == ride_id,
            Booking.passenger_id == current_user.id,
            Booking.status.in_([BookingStatus.pending, BookingStatus.accepted]),
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="You already booked this ride")

    booking = Booking(
        ride_id=ride_id,
        passenger_id=current_user.id,
    )
    db.add(booking)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # asyncpg exposes PostgreSQL's constraint name on the original cause.
        # Do not misclassify unrelated integrity errors as duplicate bookings.
        cause = exc.orig.__cause__
        if (
            getattr(exc.orig, "sqlstate", None) == "23505"
            and getattr(cause, "constraint_name", None) == "uq_bookings_active_passenger_ride"
        ):
            raise HTTPException(status_code=409, detail="You already booked this ride") from None
        raise
    await db.refresh(booking)
    return booking


@router.patch("/{booking_id}/respond", response_model=BookingResponse)
async def respond_to_booking(
    booking_id: int,
    accept: bool,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Only drivers can respond
    if current_user.role != UserRole.driver:
        raise HTTPException(
            status_code=403, detail="Only drivers can respond to bookings"
        )

    # Read only the routing key, not booking state, before acquiring locks.
    ride_id = await db.scalar(select(Booking.ride_id).where(Booking.id == booking_id))

    if ride_id is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Always lock Ride -> Booking. Refresh any instances already in the session
    # so decisions use database state after waiting for concurrent transactions.
    result = await db.execute(
        select(Ride)
        .where(Ride.id == ride_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    ride = result.scalar_one_or_none()

    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")

    if ride.driver_id != current_user.id:
        raise HTTPException(status_code=403, detail="This is not your ride")

    result = await db.execute(
        select(Booking)
        .where(Booking.id == booking_id, Booking.ride_id == ride_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    booking = result.scalar_one_or_none()

    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.status != BookingStatus.pending:
        raise HTTPException(status_code=409, detail="Booking already responded to")

    if ride.status != RideStatus.active:
        raise HTTPException(status_code=409, detail="Ride is not active")

    if accept:
        if ride.available_seats < 1:
            raise HTTPException(status_code=409, detail="No seats available")
        booking.status = BookingStatus.accepted
        ride.available_seats -= 1
    else:
        booking.status = BookingStatus.rejected

    await db.commit()
    await db.refresh(booking)
    return booking
