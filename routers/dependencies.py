from dataclasses import dataclass
from datetime import datetime, timezone
import re

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import ALGORITHM, SECRET_KEY
from database import get_db
from models import AccountStatus, User, UserRole

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = decode_access_claims(credentials.credentials)
    except InvalidAccessToken:
        raise unauthorized from None

    result = await db.execute(select(User).where(User.id == claims.user_id))
    user = result.scalar_one_or_none()

    # Suspension (Milestone D): a suspended account and any token issued before
    # the latest suspend/reinstate (stale `tv`) get the same generic 401 as a
    # bad token, so a token holder learns nothing about the account's state.
    if (
        user is None
        or user.role != claims.role
        or user.account_status != AccountStatus.active.value
        or user.token_version != claims.token_version
    ):
        raise unauthorized

    return user


class InvalidAccessToken(Exception):
    """Any reason a bearer token is unusable; callers map it to a generic 401."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: int
    role: UserRole
    token_version: int


TOKEN_VERSION_MAX = 2**31 - 1


def decode_access_token(token: str) -> tuple[int, UserRole]:
    """Verify an HS* access token and return (user_id, role)."""
    claims = decode_access_claims(token)
    return claims.user_id, claims.role


def decode_access_claims(token: str) -> AccessClaims:
    """Verify an HS* access token and return its validated claims.

    Accepts tokens issued by python-jose (<= AST-008) and PyJWT alike: the
    compact JWS format, header and claims are identical. Tokens issued before
    Milestone D carry no `tv` claim and are read as token_version 0, which is
    every account's starting version, so they keep working until expiry
    unless the account has since been suspended or reinstated.
    """
    try:
        # The algorithm is pinned by configuration, never taken from the token.
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={
            "verify_signature": True, "verify_exp": True,
            "require": ["sub", "role", "exp"],
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
        token_version = payload.get("tv", 0)
        # type() rather than isinstance(): JSON true must not pass as 1.
        if type(token_version) is not int or not 0 <= token_version <= TOKEN_VERSION_MAX:
            raise ValueError("Invalid token version")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
        raise InvalidAccessToken from None
    return AccessClaims(user_id=user_id, role=role, token_version=token_version)


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
