from datetime import datetime, timezone
import re

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import ALGORITHM, SECRET_KEY
from database import get_db
from models import User, UserRole

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    unauthorized = HTTPException(
        status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={
            "verify_signature": True, "verify_exp": True,
            "require_sub": True, "require_role": True, "require_exp": True,
        })
        subject = payload["sub"]
        if not isinstance(subject, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", subject):
            raise ValueError("Invalid subject")
        user_id = int(subject)
        if user_id > 2**31 - 1:
            raise ValueError("Invalid subject")
        role = UserRole(payload["role"])
        now = int(datetime.now(timezone.utc).timestamp())
        if type(payload["exp"]) is not int or payload["exp"] <= now:
            raise ValueError("Invalid expiration")
        # Pre-hardening tokens lacked iat; keep them valid until their expiry.
        if "iat" in payload and (
            type(payload["iat"]) is not int
            or not 0 <= payload["iat"] <= now
            or payload["iat"] >= payload["exp"]
        ):
            raise ValueError("Invalid issued-at time")
        if "nbf" in payload and (type(payload["nbf"]) is not int or payload["nbf"] > now):
            raise ValueError("Invalid not-before time")
    except (JWTError, ValueError, TypeError, KeyError, OverflowError):
        raise unauthorized from None

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or user.role != role:
        raise unauthorized

    return user


ADMIN_FORBIDDEN_DETAIL = "Admin access required"


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Gate for every admin-only route.

    Authentication failures (missing, malformed, expired or role-mismatched
    token) stay 401 from get_current_user. An authenticated passenger/driver
    gets 403. The role is the one re-read from the database by
    get_current_user (and required to equal the signed claim), so a demoted
    admin's old token is rejected with 401, not honoured until expiry.
    """
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail=ADMIN_FORBIDDEN_DETAIL)
    return current_user
