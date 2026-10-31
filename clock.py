"""Single source of the current UTC time for lifecycle rules.

Services call ``utc_now()`` instead of ``datetime.now`` directly so time-based
rules (departure guards, future expiry jobs) share one definition and tests can
freeze time by patching ``clock.utc_now``.
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def has_departed(departure_time: datetime, now: datetime | None = None) -> bool:
    """A ride has departed once its departure time is at or before ``now``.

    The boundary is inclusive: at the exact departure instant the ride is
    treated as departed, matching ride search, which lists only rides whose
    departure is strictly in the future.
    """
    current = utc_now() if now is None else now
    return departure_time.astimezone(timezone.utc) <= current
