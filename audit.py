"""The single write path for admin_audit_log (Milestone D, M5-B).

record() adds the row to the caller's session and flushes; it never commits.
The admin action and its audit row therefore commit or roll back together.

metadata is checked before it is stored: keys are short snake_case names
that do not look like personal data, and values are ids, counts, booleans,
short codes, or lists of ids. A violation is a programming error
(ValueError), never silently dropped, so PII cannot slip in through a later
refactor without a failing test.
"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from models import AdminAuditLog

ACTIONS = frozenset({
    "user.view", "user.suspend", "user.reinstate",
    # Written only by the operator CLI (python -m scripts.grant_admin); there is
    # deliberately no HTTP route that changes a role (docs/security/ADMIN_BOOTSTRAP.md).
    "admin_granted", "admin_revoked",
})
TARGET_TYPES = frozenset({"user"})
REASON_MAX_LENGTH = 500

_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PII_KEY = re.compile(r"cnic|phone|email|name|password|address|national|token(?!_version)|dob|birth")
_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")  # ':' for actor codes like system:bootstrap
REASON_MIN_LENGTH = 3
# Seven or more digits, allowing the separators people type in CNICs and
# phone numbers. A reason is stored and shown to other admins; it must not
# become a second copy of someone's identity or contact number.
_LONG_DIGIT_RUN = re.compile(r"[0-9](?:[\s\-./]*[0-9]){6,}")


def validate_reason(value: str) -> str:
    """Trim and check a free-text admin reason (API body and operator CLI alike)."""
    if type(value) is not str:
        raise ValueError("Reason is required")
    value = value.strip()
    if len(value) < REASON_MIN_LENGTH:
        raise ValueError("Reason is required")
    if len(value) > REASON_MAX_LENGTH:
        raise ValueError("Reason is too long")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("Reason contains control characters")
    if _LONG_DIGIT_RUN.search(value):
        raise ValueError("Reason must not contain ID or phone numbers")
    return value
_MAX_LIST = 10_000


def _check_value(key: str, value):
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        if not _CODE.fullmatch(value):
            raise ValueError(f"audit metadata {key!r}: strings must be short lowercase codes")
        return
    if type(value) is list:
        if len(value) > _MAX_LIST or any(type(item) is not int for item in value):
            raise ValueError(f"audit metadata {key!r}: lists may hold only integer ids")
        return
    raise ValueError(f"audit metadata {key!r}: unsupported type {type(value).__name__}")


def validate_metadata(metadata: dict | None) -> dict:
    metadata = dict(metadata or {})
    for key, value in metadata.items():
        if type(key) is not str or not _KEY.fullmatch(key) or _PII_KEY.search(key):
            raise ValueError(f"audit metadata key {key!r} is not allowed")
        _check_value(key, value)
    return metadata


async def record(
    db: AsyncSession, *, actor_id: int | None, action: str, target_type: str,
    target_id: int, reason: str | None = None, metadata: dict | None = None,
) -> AdminAuditLog:
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    if target_type not in TARGET_TYPES:
        raise ValueError(f"unknown audit target type {target_type!r}")
    if type(target_id) is not int:
        raise ValueError("audit target_id must be an int")
    if reason is not None and len(reason) > REASON_MAX_LENGTH:
        raise ValueError("audit reason too long")
    entry = AdminAuditLog(
        actor_id=actor_id, action=action, target_type=target_type, target_id=target_id,
        reason=reason, details=validate_metadata(metadata),
    )
    db.add(entry)
    await db.flush()
    return entry
