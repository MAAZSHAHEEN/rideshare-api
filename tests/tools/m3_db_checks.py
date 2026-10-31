"""Milestone 3 database checks on disposable PostgreSQL databases (RC-stress).

Usage (local, disposable databases only; never point this at staging/production):

    export PGPASSWORD=...  M3_PG="postgresql://USER@localhost:5432"  M3_DB_PREFIX=rs_stress_m3
    python tests/tools/m3_db_checks.py [check ...]      # default: all checks

Every check creates its own database(s) named ``$M3_DB_PREFIX_<check>...``,
drops them first if they exist, and prints one JSON line per result. The
script refuses to run unless the host is localhost/127.0.0.1 and every
database name starts with ``rs_stress``. Fixture data is synthetic.

Checks:
  legacy        upgrade d924a6e38f10 -> head with seeded legacy rows (mixed-case
                email twins first blocked by the f6b2d8e0c413 preflight, 0-seat
                rides, pending/accepted/rejected/cancelled bookings), then verify
                every legacy row and `alembic check`
  constraints   uq_users_email_lower, the booking partial unique index, the
                multi-seat CHECK, and FK leading-index coverage
  audit         admin_audit_log UPDATE/DELETE/TRUNCATE rejected for a non-owner,
                non-superuser app role granted ALL; superuser bypass documented
  tokens        token_version / 401 after suspend and reinstate, over HTTP on a
                migrated database
  interrupt     for every revision after d924a6e38f10: block it on a lock,
                kill -9 the alembic process mid-migration, check alembic_version,
                retry, compare the schema with a clean head (two variants:
                orphan backend terminated / orphan allowed to finish its statement)
  downgrade     head -> each supported revision -> head, with data; schema compared
                with a clean build at each revision; multi-seat downgrade refusal
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PG = os.environ.get("M3_PG", "postgresql://rideshare_ci@localhost:5432")
PREFIX = os.environ.get("M3_DB_PREFIX", "rs_stress_m3")
PASSWORD = os.environ.get("PGPASSWORD", "")
PYTHON = os.environ.get("M3_PYTHON", sys.executable)
LEGACY = "d924a6e38f10"
CHAIN = ["e3a7c41b9f52", "f6b2d8e0c413", "c1a7e0b00001", "c1a7e0b00002", "c1a7e0b00003",
         "d5e1a0b00001", "d5e1a0b00002"]
HEAD = CHAIN[-1]
PREVIOUS = dict(zip(CHAIN, [LEGACY] + CHAIN[:-1]))
# Table each revision needs a lock on (used to park it mid-migration).
BLOCK_TABLE = {"e3a7c41b9f52": "rides", "f6b2d8e0c413": "users", "c1a7e0b00001": "users",
               "c1a7e0b00002": "bookings", "c1a7e0b00003": "bookings",
               "d5e1a0b00001": "users", "d5e1a0b00002": "users"}

assert urlparse(PG).hostname in ("localhost", "127.0.0.1"), "local disposable databases only"
assert PREFIX.startswith("rs_stress"), "database prefix must start with rs_stress"
RESULTS = []


def emit(check, name, ok, **details):
    row = {"check": check, "name": name, "ok": bool(ok), **details}
    RESULTS.append(row)
    print(json.dumps(row, default=str), flush=True)


def psql(db, sql, *, check=True, user_url=None):
    url = user_url or f"{PG}/{db}"
    proc = subprocess.run(["psql", url, "-v", "ON_ERROR_STOP=1", "-qAtX", "-c", sql],
                          capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()}\nSQL: {sql[:300]}")
    return proc


def scalar(db, sql):
    return psql(db, sql).stdout.strip()


def fresh(db):
    assert db.startswith("rs_stress")
    psql("postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    psql("postgres", f'CREATE DATABASE "{db}"')


def async_url(db):
    parsed = urlparse(PG)
    return f"postgresql+asyncpg://{parsed.username}:{PASSWORD}@{parsed.hostname}:{parsed.port or 5432}/{db}"


def alembic_env(db):
    return {**os.environ, "DATABASE_URL": async_url(db), "PYTHONDONTWRITEBYTECODE": "1"}


def alembic(db, *args, check=True):
    proc = subprocess.run([PYTHON, "-m", "alembic", *args], cwd=ROOT, env=alembic_env(db),
                          capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{proc.stderr[-2000:]}")
    return proc


def version(db):
    return scalar(db, "SELECT string_agg(version_num, ',') FROM alembic_version")


def schema_dump(db):
    proc = subprocess.run(["pg_dump", f"{PG}/{db}", "--schema-only", "--no-owner", "--no-privileges",
                           "--exclude-table=alembic_version"], capture_output=True, text=True, check=True)
    lines = [line for line in proc.stdout.splitlines()
             if line.strip() and not line.startswith("--") and not line.startswith("\\")
             and not line.startswith("SET ") and "set_config" not in line]
    return "\n".join(lines)


def dump_diff(a, b):
    sa, sb = set(a.splitlines()), set(b.splitlines())
    return sorted(sa - sb)[:10], sorted(sb - sa)[:10]


LEGACY_SEED = """
INSERT INTO users (id, name, email, password, cnic, phone_number, role, created_at, updated_at) VALUES
 (1, 'Legacy Driver', 'legacy.driver@example.test', 'x', '00000-0000001-1', '+920000000001', 'driver', now(), now()),
 (2, 'Legacy Passenger', 'legacy.passenger@example.test', 'x', '00000-0000002-1', '+920000000002', 'passenger', now(), now()),
 (3, 'Legacy Twin A', 'Twin.Case@Example.test', 'x', '00000-0000003-1', '+920000000003', 'passenger', now(), now()),
 (4, 'Legacy Twin B', 'twin.case@example.test', 'x', '00000-0000004-1', '+920000000004', 'passenger', now(), now()),
 (5, 'Legacy Admin', 'legacy.admin@example.test', 'x', '00000-0000005-1', '+920000000005', 'admin', now(), now());
SELECT setval(pg_get_serial_sequence('users', 'id'), 5);
INSERT INTO rides (id, driver_id, origin, destination, departure_time, available_seats, fare_per_seat, status, created_at) VALUES
 (1, 1, 'Lahore', 'Islamabad', now() + interval '3 days', 2, 1000, 'active', now()),
 (2, 1, 'Lahore', 'Multan', now() + interval '3 days', 0, 800, 'active', now()),
 (3, 1, 'Lahore', 'Karachi', now() - interval '10 days', 0, 3000, 'completed', now()),
 (4, 1, 'Lahore', 'Sialkot', now() - interval '2 days', 1, 500, 'active', now()),
 (5, 1, 'Lahore', 'Faisalabad', now() + interval '1 day', 3, 600, 'cancelled', now());
SELECT setval(pg_get_serial_sequence('rides', 'id'), 5);
INSERT INTO bookings (id, ride_id, passenger_id, status, created_at) VALUES
 (1, 1, 2, 'accepted', now()), (2, 1, 3, 'pending', now()), (3, 1, 4, 'cancelled', now()),
 (4, 1, 4, 'pending', now()), (5, 2, 2, 'accepted', now()), (6, 3, 2, 'accepted', now()),
 (7, 4, 3, 'pending', now()), (8, 5, 2, 'cancelled', now()), (9, 2, 3, 'rejected', now());
SELECT setval(pg_get_serial_sequence('bookings', 'id'), 9);
"""
LEGACY_FINGERPRINT = (
    "SELECT md5(string_agg(row_text, '|' ORDER BY row_text)) FROM ("
    " SELECT 'u' || id || email || role::text AS row_text FROM users"
    " UNION ALL SELECT 'r' || id || available_seats || status::text || departure_time FROM rides"
    " UNION ALL SELECT 'b' || id || ride_id || passenger_id || status::text FROM bookings) t")


def legacy_db(db):
    fresh(db)
    alembic(db, "upgrade", LEGACY)
    psql(db, LEGACY_SEED)


# ------------------------------------------------------------------------------------------------
def check_legacy():
    db = f"{PREFIX}_legacy"
    legacy_db(db)
    before = scalar(db, LEGACY_FINGERPRINT)
    blocked = alembic(db, "upgrade", "head", check=False)
    stamp = version(db)
    index = scalar(db, "SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_users_email_lower'")
    message = re.search(r"CaseDuplicateEmails: (.*)", blocked.stderr)
    emit("legacy", "preflight blocks mixed-case email twins", blocked.returncode != 0
         and "CaseDuplicateEmails" in blocked.stderr and stamp == "e3a7c41b9f52" and index == "0"
         and "twin.case" not in blocked.stderr.lower(),
         exit_code=blocked.returncode, alembic_version=stamp, uq_index_present=index,
         message=message.group(1) if message else None)
    emit("legacy", "nothing rewritten by the blocked attempt", scalar(db, LEGACY_FINGERPRINT) == before)
    psql(db, "UPDATE users SET email = 'twin.case.b@example.test' WHERE id = 4")  # operator resolution
    after_fix = scalar(db, LEGACY_FINGERPRINT)
    alembic(db, "upgrade", "head")
    emit("legacy", "upgrade to head after resolving twins", version(db) == HEAD, alembic_version=version(db))
    emit("legacy", "legacy rows unchanged by the upgrade", scalar(db, LEGACY_FINGERPRINT) == after_fix)
    rows = scalar(db, "SELECT json_build_object("
                      "'seats_requested_not_1', (SELECT count(*) FROM bookings WHERE seats_requested <> 1),"
                      "'expires_at_not_null', (SELECT count(*) FROM bookings WHERE expires_at IS NOT NULL),"
                      "'status_counts', (SELECT json_object_agg(status, n) FROM (SELECT status, count(*) n FROM bookings GROUP BY status) s),"
                      "'zero_seat_rides', (SELECT count(*) FROM rides WHERE available_seats = 0),"
                      "'users_not_active', (SELECT count(*) FROM users WHERE account_status <> 'active'),"
                      "'token_version_not_0', (SELECT count(*) FROM users WHERE token_version <> 0),"
                      "'audit_rows', (SELECT count(*) FROM admin_audit_log),"
                      "'idempotency_rows', (SELECT count(*) FROM idempotency_keys),"
                      "'invalid_indexes', (SELECT count(*) FROM pg_index WHERE NOT indisvalid),"
                      "'not_validated_constraints', (SELECT count(*) FROM pg_constraint WHERE NOT convalidated))")
    data = json.loads(rows)
    emit("legacy", "legacy row shape after upgrade", data["seats_requested_not_1"] == 0
         and data["expires_at_not_null"] == 0 and data["zero_seat_rides"] == 2
         and data["users_not_active"] == 0 and data["token_version_not_0"] == 0
         and data["invalid_indexes"] == 0 and data["not_validated_constraints"] == 0, **data)
    drift = alembic(db, "check", check=False)
    emit("legacy", "alembic check: models match migrated schema", drift.returncode == 0,
         output=[line for line in (drift.stdout + drift.stderr).splitlines()
                 if "No new upgrade" in line or "FAILED" in line or "Error" in line][-1:])
    # Legacy pending on a departed ride (booking 7, NULL expires_at) reads as due for the sweep.
    due = scalar(db, "SELECT count(*) FROM bookings b JOIN rides r ON r.id = b.ride_id WHERE b.status = 'pending' "
                     "AND b.expires_at IS NULL AND r.departure_time <= now()")
    emit("legacy", "legacy pending on departed ride is due under the NULL=departure rule", due == "1", due=due)


def expect_error(db, sql, sqlstate_text, user_url=None):
    proc = psql(db, sql, check=False, user_url=user_url)
    return proc.returncode != 0 and sqlstate_text in proc.stderr, proc.stderr.strip().splitlines()[:1]


def check_constraints():
    db = f"{PREFIX}_constraints"
    legacy_db(db)
    psql(db, "UPDATE users SET email = 'twin.case.b@example.test' WHERE id = 4")
    alembic(db, "upgrade", "head")
    ok, err = expect_error(db, "INSERT INTO users (name, email, password, cnic, phone_number, role) VALUES "
                               "('Dup', 'LEGACY.DRIVER@example.test', 'x', '00000-0000099-1', '+920000000099', 'driver')",
                           "uq_users_email_lower")
    emit("constraints", "uq_users_email_lower rejects a case twin", ok, error=err)
    ok, err = expect_error(db, "INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 3, 'pending')",
                           "uq_bookings_active_passenger_ride")
    emit("constraints", "partial unique: second live booking (pending+pending) rejected", ok, error=err)
    ok, err = expect_error(db, "INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 2, 'pending')",
                           "uq_bookings_active_passenger_ride")
    emit("constraints", "partial unique: pending next to accepted rejected", ok, error=err)
    proc = psql(db, "BEGIN; UPDATE bookings SET status = 'cancelled' WHERE id = 2; "
                    "INSERT INTO bookings (ride_id, passenger_id, status) VALUES (1, 3, 'pending'); ROLLBACK", check=False)
    emit("constraints", "partial unique: new request allowed after cancellation", proc.returncode == 0,
         error=proc.stderr.strip()[:200])
    for seats, should_fail in ((0, True), (9, True), (-1, True), (1, False), (8, False)):
        proc = psql(db, f"BEGIN; INSERT INTO bookings (ride_id, passenger_id, status, seats_requested) "
                        f"VALUES (5, 3, 'cancelled', {seats}); ROLLBACK", check=False)
        failed = proc.returncode != 0 and "ck_bookings_seats_requested_range" in proc.stderr
        emit("constraints", f"multi-seat CHECK seats_requested={seats}", failed == should_fail,
             rejected=failed)
    uncovered = scalar(db, """
        SELECT coalesce(json_agg(json_build_object('table', c.conrelid::regclass::text,
               'column', a.attname, 'fk', c.conname)), '[]')
        FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
          AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.conrelid
                          AND i.indkey[0] = c.conkey[1] AND i.indisvalid)""")
    fks = scalar(db, "SELECT json_agg(c.conrelid::regclass || '.' || a.attname ORDER BY 1) FROM pg_constraint c "
                     "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
                     "WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace")
    emit("constraints", "every FK column has a valid leading index", json.loads(uncovered) == [],
         uncovered=json.loads(uncovered), foreign_keys=json.loads(fks))


def check_audit():
    db = f"{PREFIX}_audit"
    fresh(db)
    alembic(db, "upgrade", "head")
    role, secret = "rs_stress_app_role", "rs-stress-disposable-app-role"
    psql("postgres", f"DROP ROLE IF EXISTS {role}", check=False)
    psql(db, f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
             f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{secret}'; END IF; END $$; "
             f"GRANT USAGE ON SCHEMA public TO {role}; "
             f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}; "
             f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {role};")
    parsed = urlparse(PG)
    app_url = f"postgresql://{role}:{secret}@{parsed.hostname}:{parsed.port or 5432}/{db}"
    psql(db, "INSERT INTO users (name, email, password, cnic, phone_number, role) VALUES "
             "('Audit Admin', 'audit.admin@example.test', 'x', '00000-0000010-1', '+920000000010', 'admin')")
    proc = psql(db, "INSERT INTO admin_audit_log (actor_id, action, target_type, target_id) "
                    "SELECT id, 'user.view', 'user', id FROM users", user_url=app_url, check=False)
    emit("audit", "app role can INSERT", proc.returncode == 0, error=proc.stderr.strip()[:200])
    for sql, label in (("UPDATE admin_audit_log SET reason = 'x'", "UPDATE"),
                       ("DELETE FROM admin_audit_log", "DELETE"),
                       ("TRUNCATE admin_audit_log", "TRUNCATE")):
        ok, err = expect_error(db, sql, "append-only", user_url=app_url)
        emit("audit", f"app role {label} rejected (granted ALL, not owner)", ok, error=err)
    ok, err = expect_error(db, "ALTER TABLE admin_audit_log DISABLE TRIGGER ALL", "must be owner", user_url=app_url)
    emit("audit", "app role cannot disable the triggers", ok, error=err)
    ok, err = expect_error(db, "SET session_replication_role = replica", "permission denied", user_url=app_url)
    emit("audit", "app role cannot set session_replication_role", ok, error=err)
    ok, err = expect_error(db, "DELETE FROM admin_audit_log", "append-only")
    emit("audit", "owner/superuser plain DELETE also rejected by trigger", ok, error=err)
    proc = psql(db, "BEGIN; SET LOCAL session_replication_role = replica; DELETE FROM admin_audit_log; ROLLBACK",
                check=False)
    emit("audit", "SUPERUSER BYPASS (documented, expected): session_replication_role=replica skips triggers",
         proc.returncode == 0, note="superuser/owner can bypass; production app role must not be owner/superuser")
    rows = scalar(db, "SELECT count(*) FROM admin_audit_log")
    emit("audit", "rows intact after all attempts", rows == "1", rows=rows)
    psql(db, f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}; REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}; "
             f"REVOKE USAGE ON SCHEMA public FROM {role}")
    psql("postgres", f"DROP DATABASE IF EXISTS \"{db}\" WITH (FORCE)")
    psql("postgres", f"DROP ROLE IF EXISTS {role}")


TOKEN_PROBE = r'''
import asyncio, json, os
from httpx import ASGITransport, AsyncClient
from main import app
from routers.auth import create_access_token
async def main():
    ids = json.loads(os.environ["M3_IDS"])
    out = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = lambda uid, role, tv: {"Authorization": "Bearer " + create_access_token(uid, role, tv)}
        reason = {"reason": "M3 token invalidation check"}
        out["before"] = (await c.get("/bookings/me", headers=h(ids["p"], "passenger", 0))).status_code
        out["suspend"] = (await c.post(f"/admin/users/{ids['p']}/suspend", json=reason, headers=h(ids["a"], "admin", 0))).status_code
        out["old_token_after_suspend"] = (await c.get("/bookings/me", headers=h(ids["p"], "passenger", 0))).status_code
        out["tv1_token_while_suspended"] = (await c.get("/bookings/me", headers=h(ids["p"], "passenger", 1))).status_code
        out["reinstate"] = (await c.post(f"/admin/users/{ids['p']}/reinstate", json=reason, headers=h(ids["a"], "admin", 0))).status_code
        out["tv0_after_reinstate"] = (await c.get("/bookings/me", headers=h(ids["p"], "passenger", 0))).status_code
        out["tv1_after_reinstate"] = (await c.get("/bookings/me", headers=h(ids["p"], "passenger", 1))).status_code
        out["tv2_after_reinstate"] = (await c.get("/bookings/me", headers=h(ids["p"], "passenger", 2))).status_code
    print(json.dumps(out))
asyncio.run(main())
'''


def check_tokens():
    db = f"{PREFIX}_tokens"
    legacy_db(db)
    psql(db, "UPDATE users SET email = 'twin.case.b@example.test' WHERE id = 4")
    alembic(db, "upgrade", "head")
    env = {**alembic_env(db), "SECRET_KEY": "m3-disposable-signing-key", "ALGORITHM": "HS256",
           "AUTH_RATE_LIMIT_ENABLED": "false", "M3_IDS": json.dumps({"p": 2, "a": 5})}
    proc = subprocess.run([PYTHON, "-c", TOKEN_PROBE], cwd=ROOT, env=env, capture_output=True, text=True)
    if proc.returncode:
        emit("tokens", "probe ran", False, error=proc.stderr[-1500:])
        return
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    expected = {"before": 200, "suspend": 200, "old_token_after_suspend": 401, "tv1_token_while_suspended": 401,
                "reinstate": 200, "tv0_after_reinstate": 401, "tv1_after_reinstate": 401, "tv2_after_reinstate": 200}
    state = json.loads(scalar(db, "SELECT json_build_object('token_version', token_version, 'status', account_status, "
                                  "'cancelled_legacy_bookings', (SELECT json_agg(id ORDER BY id) FROM bookings WHERE passenger_id = 2 AND status = 'cancelled'), "
                                  "'audit', (SELECT json_agg(action ORDER BY id) FROM admin_audit_log)) FROM users WHERE id = 2"))
    emit("tokens", "legacy pre-tv token works, dies on suspend; reinstate needs a fresh token", out == expected
         and state["token_version"] == 2 and state["audit"] == ["user.suspend", "user.reinstate"],
         responses=out, db_state=state)


def backend_pids(db):
    return [int(p) for p in scalar(db, "SELECT string_agg(pid::text, ' ') FROM pg_stat_activity "
                                       f"WHERE datname = '{db}' AND pid <> pg_backend_pid() "
                                       "AND application_name NOT LIKE 'psql%' AND wait_event_type = 'Lock'").split()]


def check_interrupt():
    reference = f"{PREFIX}_ref_head"
    legacy_db(reference)
    psql(reference, "UPDATE users SET email = 'twin.case.b@example.test' WHERE id = 4")
    alembic(reference, "upgrade", "head")
    head_dump = schema_dump(reference)
    for revision in CHAIN:
        for variant in ("terminated", "orphan_finishes"):
            db = f"{PREFIX}_int_{revision[:8]}_{variant[:4]}"
            legacy_db(db)
            psql(db, "UPDATE users SET email = 'twin.case.b@example.test' WHERE id = 4")
            alembic(db, "upgrade", PREVIOUS[revision])
            table = BLOCK_TABLE[revision]
            # Coordinator: hold ACCESS EXCLUSIVE on the table this revision needs, in psql.
            holder = subprocess.Popen(["psql", f"{PG}/{db}", "-qAtX", "-v", "ON_ERROR_STOP=1"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, env={**os.environ, "PGAPPNAME": "psql-holder"})
            holder.stdin.write(f"BEGIN; LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE; SELECT 'locked';\n")
            holder.stdin.flush()
            assert holder.stdout.readline().strip() == "locked"
            mig = subprocess.Popen([PYTHON, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=alembic_env(db),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            waiting, deadline = [], time.time() + 25  # polled every 50 ms; lock_timeout is 5 s
            while time.time() < deadline and not waiting:
                waiting = backend_pids(db)
                time.sleep(0.05)
            mig.send_signal(signal.SIGKILL)
            mig.wait()
            stamp_during = version(db)
            if variant == "terminated":
                for pid in waiting:
                    psql(db, f"SELECT pg_terminate_backend({pid})")
            holder.stdin.write("COMMIT;\n\\q\n")
            holder.stdin.flush()
            holder.wait(timeout=10)
            time.sleep(0.5)  # let an orphan backend finish (or fail) its statement
            stamp_after_kill = version(db)
            invalid = scalar(db, "SELECT count(*) FROM pg_index WHERE NOT indisvalid")
            retry = alembic(db, "upgrade", "head", check=False)
            final = version(db) if retry.returncode == 0 else None
            only_a, only_b = dump_diff(schema_dump(db), head_dump) if retry.returncode == 0 else ([], [])
            emit("interrupt", f"kill -9 during {revision} ({variant})",
                 bool(waiting) and stamp_after_kill == PREVIOUS[revision] and retry.returncode == 0
                 and final == HEAD and not only_a and not only_b,
                 blocked_backends=len(waiting), alembic_version_after_kill=stamp_after_kill,
                 alembic_version_while_killed=stamp_during, invalid_indexes_after_kill=invalid,
                 retry_exit=retry.returncode, retry_error=retry.stderr[-400:] if retry.returncode else None,
                 final_version=final, schema_diff_vs_clean_head=[only_a, only_b])
            psql("postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


def check_downgrade():
    references = {}
    for target in [LEGACY] + CHAIN:
        ref = f"{PREFIX}_ref_{target[:8]}"
        fresh(ref)
        alembic(ref, "upgrade", target)
        references[target] = schema_dump(ref)
        psql("postgres", f'DROP DATABASE IF EXISTS "{ref}" WITH (FORCE)')
    # Multi-seat refusal first.
    db = f"{PREFIX}_down_refuse"
    legacy_db(db)
    psql(db, "UPDATE users SET email = 'twin.case.b@example.test' WHERE id = 4")
    alembic(db, "upgrade", "head")
    psql(db, "INSERT INTO bookings (ride_id, passenger_id, status, seats_requested) VALUES (1, 5, 'pending', 2)")
    refused = alembic(db, "downgrade", "c1a7e0b00002", check=False)
    emit("downgrade", "below c1a7e0b00003 refused while a 2-seat booking exists",
         refused.returncode != 0 and "MultiSeatDowngradeRefused" in refused.stderr
         and version(db) == "c1a7e0b00003"
         and scalar(db, "SELECT count(*) FROM bookings WHERE seats_requested = 2") == "1",
         alembic_version=version(db))
    psql("postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    for target in reversed([LEGACY] + CHAIN[:-1]):
        db = f"{PREFIX}_down_{target[:8]}"
        legacy_db(db)
        psql(db, "UPDATE users SET email = 'twin.case.b@example.test' WHERE id = 4")
        alembic(db, "upgrade", "head")
        psql(db, "UPDATE bookings SET status = 'expired' WHERE id = 7")  # a swept legacy request
        before = scalar(db, LEGACY_FINGERPRINT.replace("status::text", "replace(status::text, 'expired', 'cancelled')"))
        down = alembic(db, "downgrade", target, check=False)
        stamp = version(db)
        only_a, only_b = dump_diff(schema_dump(db), references[target])
        # The 'expired' enum label cannot be dropped (documented): ignore exactly that difference.
        only_a = [line for line in only_a if "'expired'" not in line]
        only_b = [line for line in only_b if not re.fullmatch(r"\s*'cancelled'\s*", line)]
        data_kept = scalar(db, LEGACY_FINGERPRINT) == before
        up = alembic(db, "upgrade", "head", check=False)
        up_a, up_b = dump_diff(schema_dump(db), references[HEAD]) if up.returncode == 0 else (["upgrade failed"], [])
        emit("downgrade", f"head -> {target} -> head", down.returncode == 0 and stamp == target
             and not only_a and not only_b and data_kept and up.returncode == 0 and version(db) == HEAD
             and not up_a and not up_b,
             alembic_version_after_downgrade=stamp, schema_diff_vs_clean=[only_a, only_b],
             legacy_rows_kept=data_kept, reupgrade_version=version(db) if up.returncode == 0 else None,
             reupgrade_schema_diff=[up_a, up_b], error=(down.stderr[-300:] if down.returncode else None))
        psql("postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


CHECKS = {"legacy": check_legacy, "constraints": check_constraints, "audit": check_audit,
          "tokens": check_tokens, "interrupt": check_interrupt, "downgrade": check_downgrade}

if __name__ == "__main__":
    selected = sys.argv[1:] or list(CHECKS)
    for name in selected:
        try:
            CHECKS[name]()
        except Exception as exc:  # report and continue with the next check
            emit(name, "check crashed", False, error=repr(exc)[-1500:])
    failed = [r for r in RESULTS if not r["ok"]]
    print(json.dumps({"summary": {"total": len(RESULTS), "passed": len(RESULTS) - len(failed),
                                  "failed": len(failed)}}))
    sys.exit(1 if failed else 0)
