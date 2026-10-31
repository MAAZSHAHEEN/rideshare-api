"""Pending-request expiry and stale ride closure (Milestone C, c1a7e0b00002).

Rules (see docs/architecture/BOOKING_SCHEMA_CHANGES.md §2):

- A pending request created by this code gets
  ``expires_at = min(created + BOOKING_PENDING_TTL_MINUTES, departure_time)``.
- Legacy rows have ``expires_at IS NULL``: they expire at the ride's
  departure, which is exactly the rule that already applied (acceptance after
  departure was 409). No legacy row is rewritten by the migration.
- The boundary is inclusive, like ``clock.has_departed``: at the expiry
  instant the request is expired.
- Enforcement is lazy and never depends on the sweep:
  * accepting an expired request is 409 ``Booking request has expired``;
  * listings show it as ``expired`` (read-only: GET never writes);
  * a passenger re-requesting a ride turns their own expired request into
    ``expired`` in the same transaction, so the partial unique index admits
    the new request.
- ``sweep()`` (``python -m jobs.expire_bookings``) persists ``expired`` for due
  requests and closes stale rides: an ``active`` ride whose departure is at
  least RIDE_AUTO_CLOSE_AFTER_HOURS in the past is ``completed`` when it has an
  accepted booking, otherwise ``cancelled``; its pending requests become
  ``expired``. Accepted bookings and seat counters are never touched.

Locking: Ride -> Booking(s) ordered by id, like every other writer. The sweep
takes ride locks with SKIP LOCKED so it never waits behind (or blocks) a user
request; a skipped ride is picked up by the next run. One transaction per ride.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import clock
import lifecycle
from models import Booking, BookingStatus, Ride, RideStatus


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}")
    return value


def pending_ttl_from_environment() -> timedelta:
    # 5 minutes .. 7 days; default 6 hours (BOOKING_LIFECYCLE.md §6.1 proposal).
    return timedelta(minutes=_int_setting("BOOKING_PENDING_TTL_MINUTES", 360, 5, 7 * 24 * 60))


def auto_close_from_environment() -> timedelta:
    # 1 hour .. 30 days after departure; default 24 hours (§6.2 proposal).
    return timedelta(hours=_int_setting("RIDE_AUTO_CLOSE_AFTER_HOURS", 24, 1, 30 * 24))


PENDING_TTL = pending_ttl_from_environment()
RIDE_AUTO_CLOSE_AFTER = auto_close_from_environment()


def pending_expires_at(created_at: datetime, departure_time: datetime,
                       ttl: timedelta | None = None) -> datetime:
    """Expiry stored on a new pending request: the earlier of TTL and departure."""
    return min(created_at + (PENDING_TTL if ttl is None else ttl), departure_time)


def effective_expires_at(booking, ride) -> datetime:
    """NULL (legacy) means "at departure"; never later than departure."""
    if booking.expires_at is None:
        return ride.departure_time
    return min(booking.expires_at, ride.departure_time)


def is_expired(booking, ride, now: datetime | None = None) -> bool:
    """A *pending* request whose effective expiry is at or before ``now``."""
    if booking.status != BookingStatus.pending:
        return False
    current = clock.utc_now() if now is None else now
    return effective_expires_at(booking, ride) <= current


def display_status(booking, ride, now: datetime | None = None) -> BookingStatus:
    """Status shown by read paths: lazily-expired pending requests read ``expired``."""
    return BookingStatus.expired if is_expired(booking, ride, now) else booking.status


def due_clause(now: datetime, departure=Ride.departure_time):
    """SQL: pending request whose effective expiry is at or before ``now``.

    Written as an OR (not COALESCE) so the expires_at branch can use the
    partial index ix_bookings_pending_expires_at.
    """
    return and_(Booking.status == BookingStatus.pending, or_(
        Booking.expires_at <= now,
        and_(Booking.expires_at.is_(None), departure <= now),
    ))


def status_filter(status: BookingStatus, now: datetime, departure=Ride.departure_time):
    """SQL filter matching ``display_status`` for listing ``?status=`` queries."""
    if status == BookingStatus.expired:
        return or_(Booking.status == BookingStatus.expired, due_clause(now, departure))
    if status == BookingStatus.pending:
        return and_(Booking.status == BookingStatus.pending,
                    func.coalesce(Booking.expires_at, departure) > now, departure > now)
    return Booking.status == status


async def expire_own_due_request(db: AsyncSession, ride: Ride, passenger_id: int,
                                 now: datetime) -> None:
    """Under the caller's ride lock: persist ``expired`` on this passenger's due
    pending request for ``ride`` (at most one exists: partial unique index)."""
    booking = await db.scalar(
        select(Booking).where(
            Booking.ride_id == ride.id, Booking.passenger_id == passenger_id,
            due_clause(now, ride.departure_time),
        ).with_for_update().execution_options(populate_existing=True)
    )
    if booking is not None:
        lifecycle.apply_booking_transition(ride, booking, "expire")


@dataclass
class SweepResult:
    expired_requests: int = 0
    rides_completed: int = 0
    rides_cancelled: int = 0
    rides_skipped_locked: int = 0

    def as_lines(self) -> list[str]:
        return [f"{name}={value}" for name, value in self.__dict__.items()]


async def _lock_ride(db: AsyncSession, ride_id: int) -> Ride | None:
    return await db.scalar(
        select(Ride).where(Ride.id == ride_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )


async def expire_due_requests(db: AsyncSession, now: datetime, result: SweepResult,
                              batch_size: int = 100) -> None:
    last_ride_id = 0
    while True:
        ride_ids = (await db.scalars(
            select(Booking.ride_id).join(Ride, Ride.id == Booking.ride_id)
            .where(due_clause(now), Booking.ride_id > last_ride_id)
            .group_by(Booking.ride_id).order_by(Booking.ride_id).limit(batch_size)
        )).all()
        await db.rollback()  # end the read-only snapshot before taking locks
        if not ride_ids:
            return
        for ride_id in ride_ids:
            last_ride_id = ride_id
            try:
                ride = await _lock_ride(db, ride_id)
                if ride is None:
                    result.rides_skipped_locked += 1
                    await db.rollback()
                    continue
                due = (await db.scalars(
                    select(Booking).where(Booking.ride_id == ride_id,
                                          due_clause(now, ride.departure_time))
                    .order_by(Booking.id).with_for_update()
                    .execution_options(populate_existing=True)
                )).all()
                for booking in due:
                    lifecycle.apply_booking_transition(ride, booking, "expire")
                await db.commit()
                result.expired_requests += len(due)
            except Exception:
                await db.rollback()
                raise


async def close_stale_rides(db: AsyncSession, now: datetime, result: SweepResult,
                            close_after: timedelta | None = None, batch_size: int = 100) -> None:
    cutoff = now - (RIDE_AUTO_CLOSE_AFTER if close_after is None else close_after)
    last_ride_id = 0
    while True:
        ride_ids = (await db.scalars(
            select(Ride.id).where(Ride.status == RideStatus.active,
                                  Ride.departure_time <= cutoff, Ride.id > last_ride_id)
            .order_by(Ride.id).limit(batch_size)
        )).all()
        await db.rollback()
        if not ride_ids:
            return
        for ride_id in ride_ids:
            last_ride_id = ride_id
            try:
                ride = await _lock_ride(db, ride_id)
                if ride is None:
                    result.rides_skipped_locked += 1
                    await db.rollback()
                    continue
                # Re-check after the lock: a driver may have closed it meanwhile.
                if ride.status != RideStatus.active or ride.departure_time > cutoff:
                    await db.rollback()
                    continue
                live = (await db.scalars(
                    select(Booking).where(
                        Booking.ride_id == ride_id,
                        Booking.status.in_([BookingStatus.pending, BookingStatus.accepted]),
                    ).order_by(Booking.id).with_for_update()
                    .execution_options(populate_existing=True)
                )).all()
                accepted = 0
                for booking in live:
                    if booking.status == BookingStatus.pending:
                        lifecycle.apply_booking_transition(ride, booking, "expire")
                        result.expired_requests += 1
                    else:
                        accepted += 1
                # Completion happens only after departure (product rule); the
                # cutoff is departure + grace. No accepted seat: nobody rode.
                action = "complete" if accepted else "cancel"
                ride.status = lifecycle.ride_target(ride.status, action)
                await db.commit()
                if accepted:
                    result.rides_completed += 1
                else:
                    result.rides_cancelled += 1
            except Exception:
                await db.rollback()
                raise


async def sweep(db: AsyncSession, now: datetime | None = None, *,
                close_after: timedelta | None = None, batch_size: int = 100) -> SweepResult:
    """Idempotent: a second run with the same ``now`` changes nothing."""
    current = clock.utc_now() if now is None else now
    result = SweepResult()
    await close_stale_rides(db, current, result, close_after, batch_size)
    await expire_due_requests(db, current, result, batch_size)
    return result
