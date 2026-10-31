"""Admin-only API surface (Milestone 5 foundation, Milestone D endpoints).

Every admin route must be registered on ``router`` (or another router built
by ``admin_router``) so the ``require_admin`` guard applies to it without
relying on each endpoint remembering a dependency.
``tests/test_admin_guard.py`` fails if any ``/admin`` route on the app is
reachable without ``require_admin``.

Endpoints (Milestone D):
- GET  /admin/users/{user_id}            masked account view (no raw CNIC/phone/email)
- POST /admin/users/{user_id}/suspend    reason required; revokes tokens; cancels future rides/bookings
- POST /admin/users/{user_id}/reinstate  reason required; revokes tokens again (user must log in)
"""

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, UserRole
from privacy import mask_cnic, mask_email, mask_phone
from routers.dependencies import require_admin
from services import accounts as account_service

ADMIN_PREFIX = "/admin"
REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 500
USER_ID = Path(ge=1, le=2**31 - 1)

# Seven or more digits, allowing the separators people type in CNICs and
# phone numbers. A reason is stored and shown to other admins; it must not
# become a second copy of someone's identity or contact number.
_LONG_DIGIT_RUN = re.compile(r"[0-9](?:[\s\-./]*[0-9]){6,}")


def admin_router(prefix: str = "", **kwargs) -> APIRouter:
    """Build a router whose every route requires an authenticated admin."""
    dependencies = [Depends(require_admin), *kwargs.pop("dependencies", [])]
    return APIRouter(
        prefix=f"{ADMIN_PREFIX}{prefix}",
        dependencies=dependencies,
        tags=kwargs.pop("tags", ["Admin"]),
        **kwargs,
    )


router = admin_router()


class AccountStatusChange(BaseModel):
    reason: str = Field(min_length=REASON_MIN_LENGTH, max_length=REASON_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def _reason_is_meaningful_and_pii_free(cls, value: str) -> str:
        value = value.strip()
        if len(value) < REASON_MIN_LENGTH:
            raise ValueError("Reason is required")
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("Reason contains control characters")
        if _LONG_DIGIT_RUN.search(value):
            raise ValueError("Reason must not contain ID or phone numbers")
        return value


class AdminUserResponse(BaseModel):
    id: int
    name: str
    email_masked: str
    cnic_masked: str
    phone_masked: str
    role: UserRole
    account_status: str
    created_at: datetime | None

    @classmethod
    def from_user(cls, user: User) -> "AdminUserResponse":
        return cls(
            id=user.id, name=user.name, email_masked=mask_email(user.email),
            cnic_masked=mask_cnic(user.cnic), phone_masked=mask_phone(user.phone_number),
            role=user.role, account_status=user.account_status, created_at=user.created_at,
        )


class SuspensionResponse(BaseModel):
    user: AdminUserResponse
    cancelled_ride_ids: list[int]
    cancelled_booking_ids: list[int]
    # Departed but not completed/cancelled: left untouched for manual follow-up.
    departed_active_ride_ids: list[int]


@router.get("/users/{user_id}", response_model=AdminUserResponse)
async def get_user(user_id: int = USER_ID, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return AdminUserResponse.from_user(user)


@router.post("/users/{user_id}/suspend", response_model=SuspensionResponse)
async def suspend_user(
    change: AccountStatusChange,
    user_id: int = USER_ID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    try:
        user, effects = await account_service.suspend_user(db, admin, user_id)
        response = SuspensionResponse(
            user=AdminUserResponse.from_user(user),
            cancelled_ride_ids=effects.cancelled_ride_ids,
            cancelled_booking_ids=effects.cancelled_booking_ids,
            departed_active_ride_ids=effects.departed_active_ride_ids,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return response


@router.post("/users/{user_id}/reinstate", response_model=AdminUserResponse)
async def reinstate_user(
    change: AccountStatusChange,
    user_id: int = USER_ID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    try:
        user = await account_service.reinstate_user(db, admin, user_id)
        response = AdminUserResponse.from_user(user)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return response
