from datetime import datetime, timedelta, timezone
from functools import lru_cache
import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
import jwt
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from config import ACCESS_TOKEN_EXPIRE_MINUTES, ALGORITHM, SECRET_KEY
from database import get_db
from models import AccountStatus, User, UserRole
from schemas import PASSWORD_MAX_BYTES, Token, UserCreate, UserLogin, UserResponse
from auth_rate_limit import check_login_identity

router = APIRouter(prefix="/auth", tags=["Auth"])

INVALID_LOGIN_DETAIL = "Invalid email or password"

# Product decision D6 (open, docs/architecture/TRUST_AND_SAFETY.md §5.2): what a
# suspended account sees after entering the CORRECT password. Default: the same
# generic 401 as a wrong password, so login reveals nothing. To show the owner
# "Account suspended" instead, change only this line to
#     SUSPENDED_LOGIN_RESPONSE = (403, "Account suspended")
# (safe against enumeration because the password is verified first).
SUSPENDED_LOGIN_RESPONSE: tuple[int, str] = (401, INVALID_LOGIN_DETAIL)


# --- Helpers ---
def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if not encoded or len(encoded) > PASSWORD_MAX_BYTES:
        raise ValueError("Password must contain between 1 and 72 UTF-8 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("ascii")


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    # Lazily generated in the verification worker, never on the event loop.
    return hash_password(secrets.token_urlsafe(32))


def verify_password(plain: str, hashed: str | None) -> bool:
    try:
        encoded = plain.encode("utf-8")
        if not encoded or len(encoded) > PASSWORD_MAX_BYTES:
            return False
        candidate = hashed if hashed is not None else _dummy_password_hash()
        valid = bcrypt.checkpw(encoded, candidate.encode("ascii"))
        return hashed is not None and valid
    except (ValueError, UnicodeError):
        # Corrupt stored hashes and unsupported input fail authentication cleanly.
        return False


def create_access_token(user_id: int, role: str, token_version: int = 0) -> str:
    if type(user_id) is not int or not 1 <= user_id <= 2**31 - 1:
        raise ValueError("Invalid user ID")
    if type(token_version) is not int or not 0 <= token_version <= 2**31 - 1:
        raise ValueError("Invalid token version")
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id), "role": UserRole(role).value, "tv": token_version,
        "iat": now, "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def email_matches(email: str):
    """Case-insensitive email predicate served by the uq_users_email_lower index."""
    return func.lower(User.email) == func.lower(email)


async def find_login_user(db: AsyncSession, email: str):
    """Return the single account for a login email, or None when absent or ambiguous.

    Before migration f6b2d8e0c413 creates uq_users_email_lower, legacy data may hold
    case twins (two rows equal under lower()). scalar_one_or_none() would raise and
    turn login into HTTP 500. Prefer the row whose stored email matches exactly
    (unique via ix_users_email); otherwise an ambiguous match fails as a normal 401.
    """
    rows = (await db.execute(
        select(User)
        .where(email_matches(email))
        .order_by((User.email == email).desc(), User.id)
        .limit(2)
    )).scalars().all()
    if len(rows) == 1:
        return rows[0]
    if len(rows) == 2 and rows[0].email == email and rows[1].email != email:
        return rows[0]
    return None


# --- Routes ---
@router.post("/register", response_model=UserResponse, status_code=201)
async def register(user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    # Check duplicate email
    # Compare in PostgreSQL with lower() so the lookup matches uq_users_email_lower,
    # including legacy rows stored before email normalisation.
    # limit(1): legacy case twins (possible before f6b2) must not raise MultipleResultsFound.
    result = await db.execute(select(User.id).where(email_matches(user_data.email)).limit(1))
    if result.first() is not None:
        raise HTTPException(status_code=409, detail="Registration details already in use")

    # Check duplicate CNIC
    result = await db.execute(select(User).where(User.cnic == user_data.cnic))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Registration details already in use")

    new_user = User(
        name=user_data.name,
        email=user_data.email,
        cnic=user_data.cnic,
        phone_number=user_data.phone_number,
        role=user_data.role,
        password=await run_in_threadpool(hash_password, user_data.password),
    )
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Only the existing PostgreSQL email/CNIC uniqueness rules are conflicts.
        cause = exc.orig.__cause__
        if (
            getattr(exc.orig, "sqlstate", None) == "23505"
            and getattr(cause, "constraint_name", None) in {"ix_users_email", "uq_users_email_lower", "users_cnic_key"}
        ):
            raise HTTPException(
                status_code=409, detail="Registration details already in use"
            ) from None
        raise
    await db.refresh(new_user)
    return new_user


@router.post("/login", response_model=Token)
async def login(user_data: UserLogin, request: Request, db: AsyncSession = Depends(get_db)):
    await check_login_identity(request, user_data.email)
    user = await find_login_user(db, user_data.email)

    valid_password = await run_in_threadpool(
        verify_password, user_data.password, user.password if user else None,
    )
    if not user or not valid_password:
        raise HTTPException(
            status_code=401, detail=INVALID_LOGIN_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Checked only after the password, so the response timing and the answer
    # for a wrong password are identical for active and suspended accounts.
    if user.account_status != AccountStatus.active.value:
        status_code, detail = SUSPENDED_LOGIN_RESPONSE
        raise HTTPException(
            status_code=status_code, detail=detail,
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
        )

    token = create_access_token(
        user_id=user.id, role=user.role, token_version=user.token_version,
    )
    return {"access_token": token, "token_type": "bearer"}
