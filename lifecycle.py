"""Explicit ride and booking state machines (AST-015).

Every status change made by the services goes through the tables below, so the
allowed moves, their HTTP conflict messages and their seat-accounting effect
are defined in one place. The services keep the existing guard *order*
(authorisation and lookups first, then lifecycle) and the existing 409 detail
strings, which are part of the API contract the frontend displays.

Callers must already hold the Ride row lock (and the Booking row lock where a
booking changes) before calling these helpers: they judge the state they are
given and never re-read it. See docs/architecture/BOOKING_LIFECYCLE.md.
"""

import os
from datetime import datetime
from typing import Literal

from fastapi import HTTPException

import clock
from models import BookingStatus, Ride, RideStatus

RideAction = Literal["complete", "cancel"]
BookingAction = Literal["accept", "reject", "cancel", "expire"]

# 409 detail strings. Unchanged from the pre-AST-015 services (API contract).
RIDE_NOT_ACTIVE = "Ride is not active"
RIDE_DEPARTED = "Ride has already departed"
NO_SEATS = "No seats available"
# c1a7e0b00003: some seats remain, but fewer than the request holds.
NOT_ENOUGH_SEATS = "Not enough seats available"
ALREADY_RESPONDED = "Booking already responded to"
NOT_CANCELLABLE = "Booking cannot be cancelled"
BOOKING_EXPIRED = "Booking request has expired"  # c1a7e0b00002, see booking_expiry.py

RIDE_TRANSITIONS: dict[tuple[RideStatus, RideAction], RideStatus] = {
    (RideStatus.active, "complete"): RideStatus.completed,
    (RideStatus.active, "cancel"): RideStatus.cancelled,
}

# "cancel" covers the passenger withdrawing and the cascade when the driver
# cancels the ride (pending and accepted) or completes it (pending only).
BOOKING_TRANSITIONS: dict[tuple[BookingStatus, BookingAction], BookingStatus] = {
    (BookingStatus.pending, "accept"): BookingStatus.accepted,
    (BookingStatus.pending, "reject"): BookingStatus.rejected,
    (BookingStatus.pending, "cancel"): BookingStatus.cancelled,
    (BookingStatus.accepted, "cancel"): BookingStatus.cancelled,
    # Pending-request expiry (booking_expiry.py): system only, no seat effect.
    (BookingStatus.pending, "expire"): BookingStatus.expired,
}

# Direction of the change to ride.available_seats for each allowed booking
# transition, multiplied by the booking's seats_requested (c1a7e0b00003). Seats
# are debited only on acceptance and restored only when that known debit is
# undone, so every restoration returns exactly what was taken.
SEAT_EFFECT: dict[tuple[BookingStatus, BookingAction], int] = {
    (BookingStatus.pending, "accept"): -1,
    (BookingStatus.accepted, "cancel"): +1,
}

_BOOKING_CONFLICT: dict[BookingAction, str] = {
    "accept": ALREADY_RESPONDED,
    "reject": ALREADY_RESPONDED,
    "cancel": NOT_CANCELLABLE,
    "expire": ALREADY_RESPONDED,
}


def conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def ride_target(status: RideStatus, action: RideAction) -> RideStatus:
    """Return the ride's next status or raise 409 for an illegal move."""
    try:
        return RIDE_TRANSITIONS[(status, action)]
    except KeyError:
        raise conflict(RIDE_NOT_ACTIVE) from None


def booking_target(status: BookingStatus, action: BookingAction) -> BookingStatus:
    """Return the booking's next status or raise 409 for an illegal move."""
    try:
        return BOOKING_TRANSITIONS[(status, action)]
    except KeyError:
        raise conflict(_BOOKING_CONFLICT[action]) from None


def require_ride_active(ride: Ride) -> None:
    """Booking writes are only possible while the ride itself is active."""
    if ride.status != RideStatus.active:
        raise conflict(RIDE_NOT_ACTIVE)


def require_not_departed(ride: Ride, now: datetime | None = None) -> None:
    if clock.has_departed(ride.departure_time, now):
        raise conflict(RIDE_DEPARTED)


# Schema/DB hard bound is 8 (schemas.MAX_SEATS_PER_BOOKING, DB CHECK). The
# runtime limit can only lower it: BOOKING_MAX_SEATS=1 switches multi-seat
# requests off without a code revert (rollout/rollback lever, see
# docs/architecture/BOOKING_SCHEMA_CHANGES.md §3). Validated at import.
HARD_MAX_SEATS = 8


def max_seats_from_environment() -> int:
    raw = os.getenv("BOOKING_MAX_SEATS", str(HARD_MAX_SEATS))
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError("BOOKING_MAX_SEATS must be an integer") from None
    if not 1 <= value <= HARD_MAX_SEATS:
        raise RuntimeError(f"BOOKING_MAX_SEATS must be between 1 and {HARD_MAX_SEATS}")
    return value


MAX_SEATS = max_seats_from_environment()


def seat_count(booking) -> int:
    """Seats a booking holds once accepted. A transient ORM object not yet
    flushed (column default not applied) counts as 1, like the DB default."""
    seats = getattr(booking, "seats_requested", None)
    if seats is None:
        return 1
    if type(seats) is not int or not 1 <= seats <= HARD_MAX_SEATS:
        raise ValueError("booking.seats_requested out of range")
    return seats


def seats_conflict(available: int) -> HTTPException:
    """409 for a request/acceptance needing more seats than ``available``."""
    return conflict(NO_SEATS if available < 1 else NOT_ENOUGH_SEATS)


def require_seats_available(ride: Ride, seats: int) -> None:
    if ride.available_seats < seats:
        raise seats_conflict(ride.available_seats)


def apply_booking_transition(ride: Ride, booking, action: BookingAction) -> None:
    """Move ``booking`` and adjust the locked ride's seat counter atomically.

    The change is ``SEAT_EFFECT * seats_requested``. Raises 409 without
    mutating anything when the move is illegal or would drive
    ``available_seats`` below zero.
    """
    target = booking_target(booking.status, action)
    delta = SEAT_EFFECT.get((booking.status, action), 0) * seat_count(booking)
    if ride.available_seats + delta < 0:
        raise seats_conflict(ride.available_seats)
    ride.available_seats += delta
    booking.status = target


def accepted_cancellation_closed(ride: Ride, now: datetime | None = None) -> bool:
    """Cancellation cut-off for an ACCEPTED booking: closed from departure on.

    After departure an accepted seat has been used or the passenger is a
    no-show; restoring the seat to a departed ride is meaningless and would
    hide the no-show. Pending requests can still be withdrawn at any time.
    The cut-off equals the departure instant (inclusive, like has_departed);
    an earlier cut-off window is a product decision (see BOOKING_LIFECYCLE.md).
    """
    return clock.has_departed(ride.departure_time, now)
