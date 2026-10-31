"""Grant or revoke the admin role from an operator shell. There is no HTTP route for this.

    # First admin (only while no admin account exists at all):
    python -m scripts.grant_admin --bootstrap --email ops-admin@example.com \
        --reason "Initial admin approved by Maaz" --confirm

    # Every later grant / revoke names an existing admin and proves it with
    # that admin's password:
    python -m scripts.grant_admin --email new-admin@example.com \
        --actor-email ops-admin@example.com --reason "Second on-call admin" --confirm
    python -m scripts.grant_admin --revoke --to-role passenger --email old-admin@example.com \
        --actor-email ops-admin@example.com --reason "Left the team" --confirm

Authorization (docs/security/ADMIN_BOOTSTRAP.md):
- ``--confirm`` is required; without it nothing is read or written.
- ``ADMIN_BOOTSTRAP_TOKEN`` (>= 32 characters) must be set in the operator's
  environment and the same value typed at the prompt (or given as the first
  line of stdin with ``--secrets-stdin``). It is never accepted as a flag,
  so it does not land in shell history or ``ps``.
- ``--bootstrap`` refuses unless zero admin accounts exist (suspended admins
  count). Later grants and every revoke need ``--actor-email`` of an active
  admin plus that admin's password, and an admin can never change their own
  role.
- A DATABASE_URL that does not point at a local database is treated as
  production and refused unless ``--i-understand-production`` is given.
  Production use needs Maaz's written approval first.

Effects, in ONE transaction (serialized across runs by an advisory lock):
the role change, ``token_version + 1`` (every token issued before the change
stops working; the user must log in again), and one ``admin_audit_log`` row
(``admin_granted`` / ``admin_revoked``; ``actor_id`` NULL plus
``metadata.actor = "system:bootstrap"`` for bootstrap, else the acting
admin's id). Any failure rolls all of it back.

Exit codes: 0 done, 1 refused (state precondition), 2 usage, 3 not authorized,
4 production-looking database without ``--i-understand-production``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hmac
import os
import sys
from datetime import datetime, timezone

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_UNAUTHORIZED, EXIT_PRODUCTION = 0, 1, 2, 3, 4

TOKEN_ENV = "ADMIN_BOOTSTRAP_TOKEN"
TOKEN_MIN_LENGTH = 32
_PLACEHOLDER_PREFIXES = ("change", "your", "replace", "example", "placeholder", "xxx", "todo")
# Single-bigint advisory key ("RSADMIN\0"). The job locks use the two-int4 form,
# a separate key space in PostgreSQL, so they can never collide with this one.
ADMIN_ROLE_LOCK_KEY = 0x5253_4144_4D49_4E00
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "db", "postgres"})
PRODUCTION_ENV_VARS = ("APP_ENV", "ENVIRONMENT", "ENV", "RIDESHARE_ENV")
PRODUCTION_ENV_VALUES = frozenset({"production", "prod", "live"})
ROLE_CHOICES = ("passenger", "driver")


class Refused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # never sys.exit from inside run()
        raise Refused(EXIT_USAGE, message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m scripts.grant_admin", description=__doc__.split("\n\n")[0])
    parser.add_argument("--email", required=True, help="Target account email (case-insensitive)")
    parser.add_argument("--reason", required=True, help="Why (3-500 chars, no names or ID numbers)")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Grant the FIRST admin; refused if any admin account exists")
    parser.add_argument("--actor-email", help="Existing active admin performing a later grant/revoke")
    parser.add_argument("--revoke", action="store_true", help="Remove the admin role instead")
    parser.add_argument("--to-role", choices=ROLE_CHOICES,
                        help="Role after --revoke (default passenger)")
    parser.add_argument("--confirm", action="store_true", help="Required: actually do it")
    parser.add_argument("--secrets-stdin", action="store_true",
                        help="Read the bootstrap token (and actor password) as lines from stdin")
    parser.add_argument("--i-understand-production", action="store_true",
                        help="Allow a non-local DATABASE_URL. Needs Maaz's written approval.")
    return parser


def looks_like_production(url: str, env) -> str | None:
    """Return why ``url``/``env`` look like production, or None for a local database.

    Fails closed: anything that is not clearly a local database counts.
    """
    for var in PRODUCTION_ENV_VARS:
        if (env.get(var) or "").strip().lower() in PRODUCTION_ENV_VALUES:
            return f"{var} says production"
    from sqlalchemy.engine import make_url
    try:
        parsed = make_url(url)
    except Exception:
        return "DATABASE_URL cannot be parsed"
    host = (parsed.host or "").strip().lower()
    if host and host not in LOCAL_HOSTS and not host.endswith(".localhost"):
        return "DATABASE_URL host is not local"
    if "prod" in (parsed.database or "").lower():
        return "database name contains 'prod'"
    return None


def _check_configured_token(value: str | None) -> str:
    if (value is None or len(value) < TOKEN_MIN_LENGTH or value != value.strip()
            or value.lower().startswith(_PLACEHOLDER_PREFIXES)):
        raise Refused(EXIT_UNAUTHORIZED,
                      f"{TOKEN_ENV} is not set, too short (< {TOKEN_MIN_LENGTH}) or a placeholder")
    return value


def _verify_password(plain: str, hashed: str | None) -> bool:
    import bcrypt
    try:
        encoded = (plain or "").encode("utf-8")
        if not encoded or len(encoded) > 72 or not hashed:
            return False
        return bcrypt.checkpw(encoded, hashed.encode("ascii"))
    except (ValueError, UnicodeError):
        return False


async def _find_user(db, email: str):
    """The single account whose email equals ``email`` case-insensitively, else None."""
    from sqlalchemy import func, select
    from models import User
    rows = (await db.scalars(
        select(User).where(func.lower(User.email) == func.lower(email.strip()))
        .order_by(User.id).limit(2)
    )).all()
    return rows[0] if len(rows) == 1 else None  # ambiguous legacy twins are refused


async def _lock(db, user_id: int, *, exclusive: bool):
    from sqlalchemy import select
    from models import User
    query = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    query = query.with_for_update(key_share=True) if exclusive else query.with_for_update(read=True)
    return await db.scalar(query)


async def apply_change(engine, args, reason: str, actor_password: str | None) -> dict:
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    import audit
    from models import AccountStatus, User, UserRole

    active = AccountStatus.active.value
    async with AsyncSession(engine, expire_on_commit=False) as db:
        async with db.begin():
            # One role change at a time: the zero-admin check below cannot race.
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMIN_ROLE_LOCK_KEY})
            target = await _find_user(db, args.email)
            if target is None:
                raise Refused(EXIT_REFUSED, "No single account matches --email")

            actor = None
            if args.bootstrap:
                admins = await db.scalar(
                    select(func.count()).select_from(User).where(User.role == UserRole.admin))
                if admins:
                    raise Refused(EXIT_REFUSED, "Bootstrap refused: an admin account already exists; "
                                                "use --actor-email with an existing admin")
            else:
                actor = await _find_user(db, args.actor_email)
                password_ok = actor is not None and await asyncio.to_thread(
                    _verify_password, actor_password, actor.password)
                if not password_ok or actor.role != UserRole.admin or actor.account_status != active:
                    raise Refused(EXIT_UNAUTHORIZED,
                                  "Actor is not an active admin, or the password is wrong")
                if actor.id == target.id:
                    raise Refused(EXIT_REFUSED, "An admin cannot change their own role")

            # Row locks in id order (same User-first order as the API), then re-read.
            for user_id in sorted({target.id} | ({actor.id} if actor else set())):
                await _lock(db, user_id, exclusive=user_id == target.id)
            if actor is not None and (actor.role != UserRole.admin or actor.account_status != active):
                raise Refused(EXIT_UNAUTHORIZED, "Actor is not an active admin")

            previous_role = UserRole(target.role)
            if args.revoke:
                if previous_role != UserRole.admin:
                    raise Refused(EXIT_REFUSED, "Target is not an admin")
                # A suspended admin may be revoked (compromise response), and stays suspended.
                new_role, action = UserRole(args.to_role or "passenger"), "admin_revoked"
            else:
                if previous_role == UserRole.admin:
                    raise Refused(EXIT_REFUSED, "Target is already an admin")
                if target.account_status != active:
                    raise Refused(EXIT_REFUSED, "Target account is suspended; reinstate it first")
                new_role, action = UserRole.admin, "admin_granted"

            target.role = new_role
            target.token_version = target.token_version + 1  # old tokens die (tv mismatch)
            target.updated_at = datetime.now(timezone.utc)
            await db.flush()
            entry = await audit.record(
                db, actor_id=actor.id if actor else None, action=action, target_type="user",
                target_id=target.id, reason=reason, metadata={
                    "actor": "system:bootstrap" if args.bootstrap else "admin_cli",
                    "mode": "bootstrap" if args.bootstrap else ("revoke" if args.revoke else "grant"),
                    "previous_role": previous_role.value, "new_role": new_role.value,
                    "token_version": target.token_version, "target_status": target.account_status,
                },
            )
            return {"action": action, "user_id": target.id, "email": target.email,
                    "previous_role": previous_role.value, "new_role": new_role.value,
                    "token_version": target.token_version, "audit_id": entry.id}


def _secret_reader(args, stdin):
    def read(prompt: str) -> str:
        if args.secrets_stdin:
            return stdin.readline().rstrip("\r\n")
        return getpass.getpass(prompt)
    return read


async def run(argv, *, env=None, stdin=None, out=None, err=None, engine=None) -> int:
    env = os.environ if env is None else env
    stdin, out, err = stdin or sys.stdin, out or sys.stdout, err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
        if args.bootstrap and (args.actor_email or args.revoke):
            raise Refused(EXIT_USAGE, "--bootstrap grants the first admin only; it takes no "
                                      "--actor-email and no --revoke")
        if not args.bootstrap and not args.actor_email:
            raise Refused(EXIT_USAGE, "--actor-email is required unless --bootstrap")
        if args.to_role and not args.revoke:
            raise Refused(EXIT_USAGE, "--to-role only applies with --revoke")
        if not args.confirm:
            raise Refused(EXIT_USAGE, "Nothing done: re-run with --confirm")

        url = env.get("DATABASE_URL") or ""
        if engine is None and not url:
            raise Refused(EXIT_USAGE, "DATABASE_URL is not set")
        why = looks_like_production(url, env)
        if why and not args.i_understand_production:
            raise Refused(EXIT_PRODUCTION, f"Refusing: {why}. Production use needs Maaz's written "
                                           "approval, then --i-understand-production")
        if why:
            print(f"WARNING: production-looking database ({why}). Proceeding only because "
                  "--i-understand-production was given; this requires Maaz's written approval.",
                  file=err)

        import audit
        try:
            reason = audit.validate_reason(args.reason)
        except ValueError as exc:
            raise Refused(EXIT_USAGE, f"--reason: {exc}") from None

        configured = _check_configured_token(env.get(TOKEN_ENV))
        read_secret = _secret_reader(args, stdin)
        supplied = read_secret("Admin bootstrap token: ")
        if not hmac.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8")):
            raise Refused(EXIT_UNAUTHORIZED, f"Token does not match {TOKEN_ENV}")
        actor_password = None if args.bootstrap else read_secret("Password for --actor-email: ")

        own_engine = engine is None
        if own_engine:
            from sqlalchemy.ext.asyncio import create_async_engine
            from sqlalchemy.pool import NullPool
            engine = create_async_engine(url, poolclass=NullPool)
        try:
            result = await apply_change(engine, args, reason, actor_password)
        finally:
            if own_engine:
                await engine.dispose()
    except Refused as exc:
        print(f"grant_admin: {exc.message}", file=err)
        return exc.code

    from privacy import mask_email
    print(f"{result['action']}: user {result['user_id']} ({mask_email(result['email'])}) "
          f"{result['previous_role']} -> {result['new_role']}; token_version "
          f"{result['token_version']} (existing tokens revoked; the user must log in again); "
          f"audit entry {result['audit_id']}", file=out)
    return EXIT_OK


def main() -> None:
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
