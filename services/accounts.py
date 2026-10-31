"""Account suspension and reinstatement (Milestone D, M5-A).

Lock order everywhere: User -> Ride(s) by id ASC -> Booking(s) by id ASC.
This extends the M4 order (Ride -> Booking) with the user row in front:

- suspend/reinstate take FOR NO KEY UPDATE on the target user row first;
- book_ride and create_ride take FOR SHARE on the caller's user row
  (``lock_active_account``) before locking any ride;
- every other ride/booking writer locks one Ride, then its Bookings.

FOR NO KEY UPDATE (not FOR UPDATE) is deliberate: it conflicts with FOR SHARE
but not with the FOR KEY SHARE that PostgreSQL takes for foreign-key checks, so
suspending a user never blocks unrelated inserts that merely reference them.

Effects of a suspension, all in the caller's single transaction:
- account_status -> 'suspended', token_version += 1 (every issued token dies);
- the user's future active rides (departure_time > now) -> cancelled, and
  their pending/accepted bookings -> cancelled (decision D5 recommended
  option, auto-cancel). A cancelled ride's available_seats is restored for
  accepted bookings, exactly as cancel_ride does;
- the user's own pending/accepted bookings on other drivers' future active
  rides -> cancelled, seats returned for accepted ones;
- departed-but-not-completed rides are NOT touched (an incident may be in
  progress); their ids are returned for manual follow-up.
Reinstatement sets 'active' and increments token_version again, so the user
must log in again; nothing cancelled is restored.

The caller owns the transaction: these functions flush but never commit, so
the audit-log row (stacked PR) is written atomically with the change.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import AccountStatus, Booking, BookingStatus, Ride, RideStatus, User

LIVE_BOOKING_STATUSES = (BookingStatus.pending, BookingStatus.accepted)


async def lock_active_account(db: AsyncSession, user_id: int) -> None:
    """Take FOR SHARE on the caller's user row and require it to still be active.

    A request authenticated just before a suspension either commits before the
    suspension locks the row (its new rows are then cancelled by it) or waits
    and is rejected here with the generic 401 used for revoked tokens.
    """
    status = await db.scalar(
        select(User.account_status).where(User.id == user_id).with_for_update(read=True)
    )
    if status != AccountStatus.active.value:
        raise HTTPException(
            status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"},
        )


@dataclass
class SuspensionEffects:
    cancelled_ride_ids: list[int] = field(default_factory=list)
    cancelled_booking_ids: list[int] = field(default_factory=list)
    departed_active_ride_ids: list[int] = field(default_factory=list)


async def _lock_target(db: AsyncSession, admin: User, user_id: int) -> User:
    if user_id == admin.id:
        raise HTTPException(status_code=403, detail="Admins cannot change their own account status")
    user = await db.scalar(
        select(User).where(User.id == user_id).with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def suspend_user(db: AsyncSession, admin: User, user_id: int) -> tuple[User, SuspensionEffects]:
    user = await _lock_target(db, admin, user_id)
    if user.account_status == AccountStatus.suspended.value:
        raise HTTPException(status_code=409, detail="User is already suspended")

    now = datetime.now(timezone.utc)
    effects = SuspensionEffects()

    # Rides whose seats change: the user's own future rides, plus the rides of
    # the user's live bookings as a passenger. Lock them all, id ASC.
    own_future_ids = set((await db.scalars(
        select(Ride.id).where(
            Ride.driver_id == user.id, Ride.status == RideStatus.active,
            Ride.departure_time > now,
        )
    )).all())
    passenger_ride_ids = set((await db.scalars(
        select(Booking.ride_id).where(
            Booking.passenger_id == user.id, Booking.status.in_(LIVE_BOOKING_STATUSES),
        )
    )).all())
    rides = {}
    if own_future_ids | passenger_ride_ids:
        rides = {ride.id: ride for ride in (await db.scalars(
            select(Ride).where(Ride.id.in_(own_future_ids | passenger_ride_ids))
            .order_by(Ride.id.asc()).with_for_update()
            .execution_options(populate_existing=True)
        )).all()}

    # Re-check under the locks: a ride may have departed, been cancelled or
    # completed while we waited.
    cancel_rides = {
        ride_id for ride_id, ride in rides.items()
        if ride.driver_id == user.id and ride.status == RideStatus.active
        and ride.departure_time > now
    }
    passenger_rides = {
        ride_id for ride_id, ride in rides.items()
        if ride.status == RideStatus.active and ride.departure_time > now
        and ride_id not in cancel_rides
    }

    if cancel_rides or passenger_rides:
        bookings = (await db.scalars(
            select(Booking).where(
                Booking.status.in_(LIVE_BOOKING_STATUSES),
                (Booking.ride_id.in_(cancel_rides))
                | ((Booking.ride_id.in_(passenger_rides)) & (Booking.passenger_id == user.id)),
            ).order_by(Booking.id.asc()).with_for_update()
            .execution_options(populate_existing=True)
        )).all()
        for booking in bookings:
            if booking.status == BookingStatus.accepted:
                rides[booking.ride_id].available_seats += 1
            booking.status = BookingStatus.cancelled
            effects.cancelled_booking_ids.append(booking.id)
        for ride_id in sorted(cancel_rides):
            rides[ride_id].status = RideStatus.cancelled
            effects.cancelled_ride_ids.append(ride_id)

    effects.departed_active_ride_ids = list((await db.scalars(
        select(Ride.id).where(
            Ride.driver_id == user.id, Ride.status == RideStatus.active,
            Ride.departure_time <= now,
        ).order_by(Ride.id.asc())
    )).all())

    user.account_status = AccountStatus.suspended.value
    user.token_version = user.token_version + 1
    await db.flush()
    return user, effects


async def reinstate_user(db: AsyncSession, admin: User, user_id: int) -> User:
    user = await _lock_target(db, admin, user_id)
    if user.account_status == AccountStatus.active.value:
        raise HTTPException(status_code=409, detail="User is not suspended")
    user.account_status = AccountStatus.active.value
    # Bump again: tokens from before the suspension must never come back.
    user.token_version = user.token_version + 1
    await db.flush()
    return user
