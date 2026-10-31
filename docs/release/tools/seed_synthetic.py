#!/usr/bin/env python3
"""Deterministic SYNTHETIC seed for RideShare staging phase 1 (no production data, ever).

Emits one SQL script on stdout; apply it with psql to a disposable/staging database whose schema was
built by `alembic upgrade <revision>` from the repo. Nothing here ships with the app (docs/release/tools/).

    python3 docs/release/tools/seed_synthetic.py --target head --users 400 --seed 20261031 > /tmp/seed.sql
    psql "$SYNTH_URL" -X -v ON_ERROR_STOP=1 -f /tmp/seed.sql

Targets:
  head  schema at d5e1a0b00002 (multi-seat, expires_at, 'expired' status, account_status, audit log)
  d924  legacy schema at d924a6e38f10 (presumed production baseline): single-seat, no expiry columns

Data (all obviously synthetic; same --seed => byte-identical SQL):
  - names "Synthetic Driver 0007" / "Synthetic Passenger 0123"; emails @example.com (RFC 2606 reserved)
  - CNIC stored canonical 13 digits starting 00000 (no Pakistani district code starts with 0, so it can
    never be a real CNIC); phones +92300 00nnnnn (one reserved-looking block, never assigned in test data)
  - one password for every synthetic account (bcrypt cost 12 of SYNTHETIC_PASSWORD below), so smoke tests
    can log in. It is a public test value, not a secret; never reuse it anywhere real.
  - rides Lahore <-> Islamabad (most; bare city names so search matches, ~15% with a landmark), plus Rawalpindi/Faisalabad/Multan/Gujranwala; departures from
    -14 days to +21 days relative to load time (now()), so expiry/stale-closure jobs have work to do
  - ride status: past -> completed / cancelled / some still 'active' (stale); future -> active / cancelled
  - bookings: pending, accepted (seats consume available_seats), rejected, cancelled, and at head:
    expired, multi-seat (1-4), pending with expires_at already past (expiry sweep candidates)
  - head: ~3% suspended users with token_version 1 and a matching admin_audit_log row
  - --email-anomalies adds preflight test rows (d924: case twins + padded twins; head: padded/NBSP/ZWSP
    twins only, since uq_users_email_lower forbids case twins). Expected preflight counts go to stderr.

Guards in the emitted SQL: refuses unless current_database() starts with rs_, synthetic or staging_synth,
and refuses if users already has rows (no mixing synthetic data with anything else).
"""
import argparse
import json
import random
import sys

SYNTHETIC_PASSWORD = "Synthetic-Only-Pass-1"
# bcrypt.hashpw(SYNTHETIC_PASSWORD, gensalt(12)) computed once; constant so the output is deterministic.
SYNTHETIC_HASH = "$2b$12$XAqAbPvBWnMGTVrv8TDEnuQrx5liC8vXKhJQHbRk6Ul0HYVyy697m"

PLACES = {
    "Lahore": ["Kalma Chowk", "Thokar Niaz Baig", "DHA Phase 5", "Johar Town", "Liberty Market"],
    "Islamabad": ["Faizabad", "F-6 Markaz", "G-9 Karachi Company", "Blue Area", "Zero Point"],
    "Rawalpindi": ["Saddar", "Pirwadhai", "Bahria Phase 7"],
    "Faisalabad": ["Clock Tower", "D Ground"],
    "Multan": ["Chowk Kumharan", "Cantt"],
    "Gujranwala": ["GT Road", "Satellite Town"],
}
ROUTES = ([("Lahore", "Islamabad"), ("Islamabad", "Lahore")] * 6
          + [("Lahore", "Rawalpindi"), ("Rawalpindi", "Lahore"), ("Lahore", "Faisalabad"),
             ("Faisalabad", "Lahore"), ("Islamabad", "Multan"), ("Lahore", "Gujranwala"),
             ("Multan", "Lahore"), ("Rawalpindi", "Islamabad")])
FARES = {frozenset(("Lahore", "Islamabad")): (2500, 4500), frozenset(("Lahore", "Rawalpindi")): (2400, 4300)}


def q(s):
    return "'" + s.replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["head", "d924"], default="head")
    ap.add_argument("--users", type=int, default=400)
    ap.add_argument("--rides-per-driver", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--email-anomalies", action="store_true")
    a = ap.parse_args()
    if not 10 <= a.users <= 99999:
        sys.exit("--users must be 10..99999")
    head = a.target == "head"
    rnd = random.Random(a.seed)
    out = []
    w = out.append
    w(f"-- synthetic seed target={a.target} users={a.users} seed={a.seed} anomalies={a.email_anomalies}")
    w("""DO $$ BEGIN
  IF current_database() !~ '^(rs_|synthetic|staging_synth)' THEN
    RAISE EXCEPTION 'seed_synthetic: refusing database % (allowed prefixes: rs_, synthetic, staging_synth)', current_database();
  END IF;
  IF EXISTS (SELECT 1 FROM users) THEN
    RAISE EXCEPTION 'seed_synthetic: users is not empty; refusing to mix synthetic data with existing rows';
  END IF;
END $$;""")
    w("BEGIN;")

    users = []  # (id, role, status)
    n_drivers = max(2, a.users // 5)
    for i in range(1, a.users + 1):
        role = "admin" if i == 1 else ("driver" if i <= n_drivers + 1 else "passenger")
        status = "suspended" if head and role != "admin" and rnd.random() < 0.03 else "active"
        users.append((i, role, status))
    cols = "id, name, email, password, cnic, phone_number, role, created_at, updated_at"
    if head:
        cols += ", account_status, token_version"
    rows = []
    for i, role, status in users:
        label = {"admin": "Admin", "driver": "Driver", "passenger": "Passenger"}[role]
        created = f"now() - interval '{rnd.randint(20, 400)} days'"
        vals = [str(i), q(f"Synthetic {label} {i:04d}"), q(f"synthetic.{role}.{i:05d}@example.com"),
                q(SYNTHETIC_HASH), q(f"00000{i:08d}"), q(f"+9230000{i:05d}"), q(role), created, created]
        if head:
            vals += [q(status), "1" if status == "suspended" else "0"]
        rows.append("(" + ", ".join(vals) + ")")
    w(f"INSERT INTO users ({cols}) VALUES\n" + ",\n".join(rows) + ";")

    expected = {}
    if a.email_anomalies:
        base = a.users
        anomalies = [  # (email, note)
            ("synthetic.twin.a@example.com\u00a0", "nbsp padded"), ("synthetic.twin.a@example.com", "clean"),
            (" synthetic.twin.b@example.com", "space padded"), ("synthetic.twin.b@example.com", "clean"),
            ("\u200bsynthetic.twin.c@example.com", "zwsp (near only)"), ("synthetic.twin.c@example.com", "clean"),
            ("synthetic.solo.pad@example.com\u3000", "ideographic pad, no twin"),
        ]
        if not head:
            anomalies += [("Synthetic.Case.D@example.com", "case"), ("synthetic.case.d@example.com", "case"),
                          ("SYNTHETIC.CASE.E@EXAMPLE.COM", "case"), ("synthetic.case.e@example.com", "case"),
                          ("synthetic.case.e@example.com ", "case + pad")]
        arows = []
        for k, (email, _) in enumerate(anomalies, start=1):
            i = base + k
            arow = [str(i), q(f"Synthetic Passenger {i:04d}"), q(email), q(SYNTHETIC_HASH), q(f"00000{i:08d}"),
                    q(f"+9230000{i:05d}"), q("passenger"), "now()", "now()"]
            if head:
                arow += [q("active"), "0"]
            arows.append("(" + ", ".join(arow) + ")")
            users.append((i, "passenger", "active"))
        w(f"INSERT INTO users ({cols}) VALUES\n" + ",\n".join(arows) + ";")
        expected = ({"blocking_groups": 0, "blocking_rows": 0, "trim_groups": 2, "trim_rows": 4,
                     "near_groups": 3, "near_rows": 6, "padded": 3, "nbsp": 1, "fmt": 1}
                    if head else
                    {"blocking_groups": 2, "blocking_rows": 4, "trim_groups": 4, "trim_rows": 9,
                     "near_groups": 5, "near_rows": 11, "padded": 4, "nbsp": 1, "fmt": 1})

    drivers = [u for u in users if u[1] == "driver"]
    passengers = [u for u in users if u[1] == "passenger" and u[2] == "active"]
    ride_rows, booking_rows, audit_rows = [], [], []
    rid = bid = 0
    counts = {"rides": 0, "bookings": 0}
    for d_id, _, _ in drivers:
        for _ in range(a.rides_per_driver):
            rid += 1
            o, dst = rnd.choice(ROUTES)
            lo, hi = FARES.get(frozenset((o, dst)), (900, 3000))
            fare = rnd.randrange(lo, hi + 1, 50)
            hours = rnd.randint(-14 * 24, 21 * 24)
            past = hours < 0
            r = rnd.random()
            status = (("completed" if r < 0.75 else "cancelled" if r < 0.9 else "active") if past
                      else ("cancelled" if r < 0.08 else "active"))
            capacity = rnd.randint(2, 6)
            taken = 0
            bk_passengers = rnd.sample(passengers, k=min(len(passengers), rnd.randint(0, 5)))
            for p_id, _, _ in bk_passengers:
                bid += 1
                seats = rnd.choice([1, 1, 1, 2, 2, 3, 4]) if head else 1
                x = rnd.random()
                if status == "cancelled":
                    bst = "cancelled" if x < 0.7 else "rejected"
                elif past:
                    bst = ("accepted" if x < 0.6 else "rejected" if x < 0.7 else "cancelled" if x < 0.8
                           else ("expired" if head and x < 0.92 else "pending"))
                else:
                    bst = "pending" if x < 0.45 else "accepted" if x < 0.8 else "rejected" if x < 0.9 else "cancelled"
                if bst == "accepted":
                    if taken + seats > capacity:
                        bst = "rejected"
                    else:
                        taken += seats
                created_h = hours - rnd.randint(2, 72)
                vals = [str(bid), str(rid), str(p_id), q(bst), f"now() + interval '{created_h} hours'"]
                if head:
                    if bst in ("pending", "expired"):
                        # mostly TTL-based; some already past expiry (sweep candidates); some legacy NULL
                        e = rnd.random()
                        exp = ("NULL" if e < 0.15 else
                               f"now() + interval '{min(created_h + 6, hours)} hours'")
                    else:
                        exp = "NULL"
                    vals += [exp, str(seats)]
                booking_rows.append("(" + ", ".join(vals) + ")")
            available = capacity - taken
            # Search matches the whole place name (trimmed, case-insensitive), so most rides use the bare
            # city; ~15% carry a pickup landmark to exercise non-matching free text.
            o_txt = o if rnd.random() < 0.85 else f"{o} ({rnd.choice(PLACES[o])})"
            d_txt = dst if rnd.random() < 0.85 else f"{dst} ({rnd.choice(PLACES[dst])})"
            ride_rows.append(f"({rid}, {d_id}, {q(o_txt)}, {q(d_txt)}, "
                             f"now() + interval '{hours} hours', {available}, {fare}, {q(status)}, "
                             f"now() + interval '{hours - rnd.randint(24, 240)} hours')")
    counts["rides"], counts["bookings"] = rid, bid
    w("INSERT INTO rides (id, driver_id, origin, destination, departure_time, available_seats, fare_per_seat, "
      "status, created_at) VALUES\n" + ",\n".join(ride_rows) + ";")
    bcols = "id, ride_id, passenger_id, status, created_at" + (", expires_at, seats_requested" if head else "")
    for chunk in range(0, len(booking_rows), 500):
        w(f"INSERT INTO bookings ({bcols}) VALUES\n" + ",\n".join(booking_rows[chunk:chunk + 500]) + ";")
    if head:
        for i, _, status in users:
            if status == "suspended":
                audit_rows.append(f"(1, 'user.suspend', 'user', {i}, 'synthetic seed', "
                                  "'{\"source\": \"seed_synthetic\"}'::jsonb)")
        if audit_rows:
            w("INSERT INTO admin_audit_log (actor_id, action, target_type, target_id, reason, metadata) VALUES\n"
              + ",\n".join(audit_rows) + ";")
    w("DO $$ BEGIN PERFORM setval(pg_get_serial_sequence(t, 'id'), m) FROM (VALUES "
      "('users', (SELECT max(id) FROM users)), ('rides', (SELECT max(id) FROM rides)), "
      "('bookings', (SELECT max(id) FROM bookings))) v(t, m); END $$;")
    w("COMMIT;")
    sys.stdout.write("\n".join(out) + "\n")
    print(json.dumps({"target": a.target, "users": len(users), **counts,
                      "suspended": sum(1 for u in users if u[2] == "suspended"),
                      "audit_rows": len(audit_rows), "expected_email_preflight": expected}), file=sys.stderr)


if __name__ == "__main__":
    main()
