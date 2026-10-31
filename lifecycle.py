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

from datetime import datetime
from typing import Literal

from fastapi import HTTPException

import clock
from models import BookingStatus, Ride, RideStatus

RideAction = Literal["complete", "cancel"]
BookingAction = Literal["accept", "reject", "cancel"]

# 409 detail strings. Unchanged from the pre-AST-015 services (API contract).
RIDE_NOT_ACTIVE = "Ride is not active"
RIDE_DEPARTED = "Ride has already departed"
NO_SEATS = "No seats available"
ALREADY_RESPONDED = "Booking already responded to"
NOT_CANCELLABLE = "Booking cannot be cancelled"
# D7 (API gaps): answering a request the passenger already withdrew. Replaces
# ALREADY_RESPONDED only for that case; accepted/rejected keep the old string.
PASSENGER_WITHDREW = "Booking was cancelled by the passenger"
# New in AST-016 (product decision 31 Oct 2026): completion only after departure.
NOT_YET_DEPARTED = "Ride cannot be completed before its departure time"

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
}

# Change to ride.available_seats for each allowed booking transition. A seat is
# debited only on acceptance and restored only when that known debit is undone.
SEAT_EFFECT: dict[tuple[BookingStatus, BookingAction], int] = {
    (BookingStatus.pending, "accept"): -1,
    (BookingStatus.accepted, "cancel"): +1,
}

_BOOKING_CONFLICT: dict[BookingAction, str] = {
    "accept": ALREADY_RESPONDED,
    "reject": ALREADY_RESPONDED,
    "cancel": NOT_CANCELLABLE,
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


def respond_target(booking, ride: Ride, action: BookingAction) -> BookingStatus:
    """Driver accept/reject: like ``booking_target`` but names a withdrawal.

    A ``cancelled`` booking on a still-``active`` ride can only have been
    cancelled by its passenger: the other paths to ``cancelled`` (driver ride
    cancellation, completion cascade) also end the ride, and those keep the
    old ``ALREADY_RESPONDED`` string. If a future flow cancels single bookings
    on an active ride (admin moderation, pending expiry), revisit this rule.
    The guard order is unchanged: booking state is judged before ride state.
    """
    if (action in ("accept", "reject") and booking.status == BookingStatus.cancelled
            and ride.status == RideStatus.active):
        raise conflict(PASSENGER_WITHDREW)
    return booking_target(booking.status, action)


def require_ride_active(ride: Ride) -> None:
    """Booking writes are only possible while the ride itself is active."""
    if ride.status != RideStatus.active:
        raise conflict(RIDE_NOT_ACTIVE)


def require_not_departed(ride: Ride, now: datetime | None = None) -> None:
    if clock.has_departed(ride.departure_time, now):
        raise conflict(RIDE_DEPARTED)


def require_departed_for_completion(ride: Ride, now: datetime | None = None) -> None:
    """A ride can be completed only at or after its scheduled departure.

    Product decision (Maaz, 31 Oct 2026): no grace window before departure.
    The boundary is the same inclusive instant as ``has_departed``: at exactly
    ``departure_time`` the ride has departed, so completion is allowed, while
    one microsecond earlier it is refused with 409 ``NOT_YET_DEPARTED``.
    """
    if not clock.has_departed(ride.departure_time, now):
        raise conflict(NOT_YET_DEPARTED)


def apply_booking_transition(ride: Ride, booking, action: BookingAction) -> None:
    """Move ``booking`` and adjust the locked ride's seat counter atomically.

    Raises 409 without mutating anything when the move is illegal or would
    drive ``available_seats`` below zero.
    """
    target = booking_target(booking.status, action)
    delta = SEAT_EFFECT.get((booking.status, action), 0)
    if ride.available_seats + delta < 0:
        raise conflict(NO_SEATS)
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
