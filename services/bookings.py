"""Booking operations using the caller's request-scoped AsyncSession.

Each operation commits once on success and refreshes its returned booking.
Creation and driver responses roll back pre-commit exceptions; cancellation
retains its protected-operation rollback. Post-commit refresh is outside those
handlers: failure there cannot undo a successful commit. The caller owns session
closure, including cancellation cleanup. No hidden/nested transactions are used.

These operations own the transaction on the supplied session, not an isolated
part of it. Commit includes unrelated pending changes and rollback discards them.
Use a clean session; these operations are not independently composable transactions.

HTTPException intentionally preserves the existing API error contract without
an additional exception translation layer.
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import lifecycle
from models import Booking, BookingStatus, Ride, User
from permissions import allowed_modes
from schemas import DriverBookingResponse, PassengerBookingResponse, PassengerSummary, RideResponse


async def list_my_bookings(
    db: AsyncSession, current_user: User, *,
    status: BookingStatus | None = None, limit: int = 20, offset: int = 0,
) -> list[PassengerBookingResponse]:
    if "passenger" not in allowed_modes(current_user.role):
        raise HTTPException(status_code=403, detail="Only passengers can list their bookings")
    query = select(Booking, Ride).join(Ride, Booking.ride_id == Ride.id).where(
        Booking.passenger_id == current_user.id,
    )
    if status is not None:
        query = query.where(Booking.status == status)
    rows = await db.execute(query.order_by(Booking.id.desc()).limit(limit).offset(offset))
    return [PassengerBookingResponse(
        id=booking.id, ride_id=booking.ride_id, passenger_id=booking.passenger_id,
        status=booking.status, ride=RideResponse.model_validate(ride),
    ) for booking, ride in rows]


async def list_ride_bookings(
    ride_id: int, db: AsyncSession, current_user: User, *,
    status: BookingStatus | None = None, limit: int = 20, offset: int = 0,
) -> list[DriverBookingResponse]:
    if "driver" not in allowed_modes(current_user.role):
        raise HTTPException(status_code=403, detail="Only drivers can list ride bookings")
    driver_id = await db.scalar(select(Ride.driver_id).where(Ride.id == ride_id))
    if driver_id is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    if driver_id != current_user.id:
        raise HTTPException(status_code=403, detail="This is not your ride")
    # Project only the passenger fields allowed in the response; no contact data.
    query = select(Booking, User.id, User.name).join(
        User, Booking.passenger_id == User.id,
    ).where(Booking.ride_id == ride_id)
    if status is not None:
        query = query.where(Booking.status == status)
    rows = await db.execute(query.order_by(Booking.id.desc()).limit(limit).offset(offset))
    return [DriverBookingResponse(
        id=booking.id, ride_id=booking.ride_id, passenger_id=booking.passenger_id,
        status=booking.status, passenger=PassengerSummary(id=user_id, name=name),
    ) for booking, user_id, name in rows]


async def cancel_booking(
    booking_id: int,
    db: AsyncSession,
    current_user: User,
) -> Booking:
    if "passenger" not in allowed_modes(current_user.role):
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
        lifecycle.require_ride_active(ride)
        lifecycle.booking_target(booking.status, "cancel")
        # Cancellation cut-off, judged after both locks: an accepted seat on a
        # departed ride is not handed back. Pending requests stay withdrawable.
        if (booking.status == BookingStatus.accepted
                and lifecycle.accepted_cancellation_closed(ride)):
            raise lifecycle.conflict(lifecycle.RIDE_DEPARTED)

        lifecycle.apply_booking_transition(ride, booking, "cancel")
        await db.commit()
    except Exception:
        # Rollback also expires ORM state; failed writes must not look persisted.
        await db.rollback()
        raise

    await db.refresh(booking)
    return booking


async def book_ride(
    ride_id: int,
    db: AsyncSession,
    current_user: User,
) -> Booking:
    try:
        # Passenger capability comes from the authenticated database account
        if "passenger" not in allowed_modes(current_user.role):
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
        if ride.driver_id == current_user.id:
            raise HTTPException(status_code=403, detail="You cannot book your own ride")
        lifecycle.require_ride_active(ride)
        # Evaluated after the row lock so a request that waited on a concurrent
        # transaction is judged against the time it actually proceeds.
        lifecycle.require_not_departed(ride)
        # A request does not debit a seat (acceptance does), but there is no
        # point queueing one for a ride that is already full.
        if ride.available_seats < 1:
            raise lifecycle.conflict(lifecycle.NO_SEATS)

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
    except Exception:
        await db.rollback()
        raise

    await db.refresh(booking)
    return booking


async def respond_to_booking(
    booking_id: int,
    accept: bool,
    db: AsyncSession,
    current_user: User,
) -> Booking:
    try:
        # Driver capability does not depend on a frontend-selected mode
        if "driver" not in allowed_modes(current_user.role):
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

        action = "accept" if accept else "reject"
        # Guard order is part of the contract: booking state, then ride state.
        lifecycle.booking_target(booking.status, action)
        lifecycle.require_ride_active(ride)

        if accept:
            # Rejection stays allowed after departure so drivers can clear
            # stale requests; acceptance would commit a seat on a departed ride.
            lifecycle.require_not_departed(ride)
        # Debits one seat on acceptance; 409 "No seats available" if none left.
        lifecycle.apply_booking_transition(ride, booking, action)

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(booking)
    return booking
