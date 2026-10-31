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
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import clock
import lifecycle
from models import Booking, BookingStatus, Ride, RideStatus, User
from permissions import allowed_modes
from schemas import RideCreate


async def get_ride(ride_id: int, db: AsyncSession) -> Ride:
    ride = await db.scalar(select(Ride).where(Ride.id == ride_id))
    if ride is None:
        raise HTTPException(status_code=404, detail="Ride not found")
    return ride


def _utc_window(departure_from: datetime | None, departure_before: datetime | None):
    """Normalise an aware [from, before) window to UTC; 422 when empty."""
    if departure_from is not None:
        departure_from = departure_from.astimezone(timezone.utc)
    if departure_before is not None:
        departure_before = departure_before.astimezone(timezone.utc)
    if (departure_from is not None and departure_before is not None
            and departure_from >= departure_before):
        raise HTTPException(status_code=422, detail="departure_from must be before departure_before")
    return departure_from, departure_before


def place_key(column):
    """SQL expression used for place matching: trimmed and case-folded.

    A later expression index must use exactly this expression to be usable
    (see docs/api/API_GAPS_CLOSED.md, G3).
    """
    return func.lower(func.btrim(column))


def _place_filter(column, value: str, name: str):
    trimmed = value.strip()
    if not trimmed:
        raise HTTPException(status_code=422, detail=f"{name} must not be blank")
    # Both sides are lowered by PostgreSQL so the folding rules are identical.
    return place_key(column) == func.lower(trimmed)


async def _count(db: AsyncSession, conditions) -> int:
    return await db.scalar(select(func.count()).select_from(Ride).where(*conditions))


async def list_my_rides(
    db: AsyncSession, current_user: User, *,
    status: RideStatus | None = None, limit: int = 20, offset: int = 0,
    departure_from: datetime | None = None, departure_before: datetime | None = None,
    when: Literal["upcoming", "departed"] | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> tuple[Sequence[Ride], int]:
    """The driver's own rides and the total matching the filters (D1, G7).

    ``when`` uses the lifecycle clock and boundary: ``upcoming`` is
    ``departure_time > now``, ``departed`` is ``departure_time <= now``. It is a
    time filter only; combine it with ``status=active`` for the UI's tabs.
    Default order (departure DESC, id DESC) is unchanged.
    """
    if "driver" not in allowed_modes(current_user.role):
        raise HTTPException(status_code=403, detail="Only drivers can list their rides")
    departure_from, departure_before = _utc_window(departure_from, departure_before)
    conditions = [Ride.driver_id == current_user.id]
    if status is not None:
        conditions.append(Ride.status == status)
    if departure_from is not None:
        conditions.append(Ride.departure_time >= departure_from)
    if departure_before is not None:
        conditions.append(Ride.departure_time < departure_before)
    if when is not None:
        now = clock.utc_now()
        conditions.append(Ride.departure_time > now if when == "upcoming"
                          else Ride.departure_time <= now)
    ordering = ((Ride.departure_time.asc(), Ride.id.asc()) if order == "asc"
                else (Ride.departure_time.desc(), Ride.id.desc()))
    rows = (await db.scalars(select(Ride).where(*conditions).order_by(*ordering)
                             .limit(limit).offset(offset))).all()
    return rows, await _count(db, conditions)


async def booking_counts_for_my_rides(
    db: AsyncSession, current_user: User, ride_ids: Sequence[int],
) -> list[dict]:
    """Per-ride booking counts by status for the caller's own rides (D2).

    IDs of rides that do not exist or belong to another driver are omitted, so
    the answer never reveals whether someone else's ride exists. Output follows
    the order of the requested IDs, duplicates removed.
    """
    if "driver" not in allowed_modes(current_user.role):
        raise HTTPException(status_code=403, detail="Only drivers can list their rides")
    wanted = list(dict.fromkeys(ride_ids))
    owned = set((await db.scalars(select(Ride.id).where(
        Ride.id.in_(wanted), Ride.driver_id == current_user.id,
    ))).all())
    counts = {ride_id: {status.value: 0 for status in BookingStatus} for ride_id in owned}
    if owned:
        rows = await db.execute(
            select(Booking.ride_id, Booking.status, func.count())
            .where(Booking.ride_id.in_(owned)).group_by(Booking.ride_id, Booking.status)
        )
        for ride_id, status, total in rows:
            counts[ride_id][status.value] = total
    return [{"ride_id": ride_id, **counts[ride_id]} for ride_id in wanted if ride_id in owned]


async def complete_ride(
    ride_id: int,
    db: AsyncSession,
    current_user: User,
) -> Ride:
    if "driver" not in allowed_modes(current_user.role):
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
        target = lifecycle.ride_target(ride.status, "complete")
        # Judged after the Ride lock and the status guard (guard order: 404,
        # 403, "Ride is not active", then time), so a completed/cancelled ride
        # keeps answering "Ride is not active" whatever the clock says.
        lifecycle.require_departed_for_completion(ride)

        # The Ride lock serializes all booking writers. Only pending bookings
        # change on completion; accepted bookings retain their consumed seats.
        pending = (await db.scalars(
            select(Booking).where(
                Booking.ride_id == ride_id, Booking.status == BookingStatus.pending,
            ).order_by(Booking.id.asc()).with_for_update()
            .execution_options(populate_existing=True)
        )).all()
        for booking in pending:
            lifecycle.apply_booking_transition(ride, booking, "cancel")
        ride.status = target
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
    if "driver" not in allowed_modes(current_user.role):
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
        target = lifecycle.ride_target(ride.status, "cancel")

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
            # Accepted bookings reverse their known debit (+1 seat), without
            # inferring an original capacity; pending ones change no seats.
            lifecycle.apply_booking_transition(ride, booking, "cancel")
        ride.status = target
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
        # Driver capability does not depend on a frontend-selected mode
        if "driver" not in allowed_modes(current_user.role):
            raise HTTPException(status_code=403, detail="Only drivers can create rides")

        departure_time = ride_data.departure_time.astimezone(timezone.utc)
        if clock.has_departed(departure_time):
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
) -> tuple[Sequence[Ride], int]:
    """Public discovery: active, future, with seats; plus the matching total.

    Places match trimmed and case-insensitively (G3). Departed rides
    (``departure_time <= now``), non-active and sold-out rides are never listed.
    """
    departure_from, departure_before = _utc_window(departure_from, departure_before)
    conditions = [
        Ride.status == RideStatus.active,
        Ride.available_seats >= min_seats,
        # Same clock and inclusive boundary as the booking guards: a ride at
        # exactly "now" has departed and is no longer listed.
        Ride.departure_time > clock.utc_now(),
    ]
    if origin is not None:
        conditions.append(_place_filter(Ride.origin, origin, "origin"))
    if destination is not None:
        conditions.append(_place_filter(Ride.destination, destination, "destination"))
    if departure_from is not None:
        conditions.append(Ride.departure_time >= departure_from)
    if departure_before is not None:
        conditions.append(Ride.departure_time < departure_before)

    result = await db.execute(
        select(Ride).where(*conditions)
        .order_by(Ride.departure_time.asc(), Ride.id.asc()).limit(limit).offset(offset)
    )
    return result.scalars().all(), await _count(db, conditions)
