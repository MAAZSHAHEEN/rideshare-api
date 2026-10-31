#!/usr/bin/env python3
"""Local self-test of the A1 read-only script. Disposable local databases ONLY (rs_a1*), synthetic data only.

    export PGPASSWORD=...   # local disposable postgres
    python3 docs/release/tools/a1_selftest.py rs_a1_d924:/tmp/a1_seed_d924.json rs_a1_head:/tmp/a1_seed_head.json

For each database (built with alembic + seed_synthetic.py --email-anomalies; the JSON is the seed's stderr):
  1. single-statement variant is byte-identical to the SELECT in the main file
  2. pg_dump (schema + data) before
  3. run the main file with psql -X -v ON_ERROR_STOP=1; parse the grid
  4. run the single-statement variant; grids must match (except the timestamp/timeouts rows)
  5. PII check: no '@', 'example.com', 'Synthetic', '+92', 13-digit run, bcrypt prefix, in either output
  6. expected email-preflight counts == seed's expected values; revision row == alembic_version;
     transaction_read_only=on; txid row empty
  7. write attempt inside the same READ ONLY transaction (INSERT/UPDATE/DELETE/CREATE) -> SQLSTATE 25006
  8. pg_stat_database tup_inserted/updated/deleted unchanged (after pg_stat_force_next_flush) and
     pg_dump after == before
"""
import json, os, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "sql/A1_readonly_baseline_and_preflight.sql"
SINGLE = ROOT / "sql/A1_readonly_single_statement.sql"
HOST = os.environ.get("A1_TEST_HOST", "localhost")
USER = os.environ.get("A1_TEST_USER", "rideshare_ci")
assert HOST in ("localhost", "127.0.0.1"), "local only"


def psql(db, *args, sql=None, check=True):
    cmd = ["psql", "-h", HOST, "-U", USER, "-d", db, "-X", "-v", "ON_ERROR_STOP=1", "-A", "-F", "\t", *args]
    if sql is not None:
        cmd += ["-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"psql failed on {db}: {r.stderr}")
    return r


def grid(text):
    rows = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0].isdigit():
            rows[int(parts[0])] = (parts[2], parts[3])
    return rows


def dump(db):
    return subprocess.run(["pg_dump", "-h", HOST, "-U", USER, "-d", db, "--no-owner", "--restrict-key=a1selftest"],
                          capture_output=True, text=True, check=True).stdout


def stats(db):
    psql(db, sql="SELECT pg_stat_force_next_flush()")
    return psql(db, "-t", sql="SELECT tup_inserted, tup_updated, tup_deleted FROM pg_stat_database "
                               "WHERE datname = current_database()").stdout.strip()


PII = [re.compile(p) for p in (r"@", r"(?i)example\.com", r"(?i)synthetic", r"\+92", r"\d{13}", r"\$2[aby]\$")]
results = []


def ok(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


main_sql = MAIN.read_text()
sel = main_sql[main_sql.index("-- A1-SELECT-BEGIN"):main_sql.index("-- A1-SELECT-END") + len("-- A1-SELECT-END")]
ok("single-statement variant byte-identical to main SELECT", sel in SINGLE.read_text())

for spec in sys.argv[1:]:
    db, seed_json = spec.split(":")
    assert db.startswith("rs_a1"), "refusing non rs_a1 database"
    exp = json.loads(Path(seed_json).read_text())["expected_email_preflight"]
    print(f"== {db}")
    before_dump, before_stats = dump(db), stats(db)
    r = psql(db, "-f", str(MAIN))
    g = grid(r.stdout)
    r2 = psql(db, "-f", str(SINGLE))
    g2 = grid(r2.stdout)
    ok(f"{db}: main file exit 0, {len(g)} rows", r.returncode == 0 and len(g) > 60)
    volatile = {902, 910}  # timestamp; timeouts differ (single-statement has no SET LOCAL)
    ok(f"{db}: single-statement grid == main grid (except {sorted(volatile | {900, 901})})",
       {k: v for k, v in g.items() if k not in volatile | {900, 901}}
       == {k: v for k, v in g2.items() if k not in volatile | {900, 901}})
    for name, text in (("main", r.stdout + r.stderr), ("single", r2.stdout + r2.stderr)):
        hits = [p.pattern for p in PII if p.search(text)]
        ok(f"{db}: {name} output has no PII patterns", not hits, ",".join(hits))
    av = psql(db, "-t", sql="SELECT version_num FROM alembic_version").stdout.strip()
    ok(f"{db}: A.version == alembic_version ({av}) and fingerprint matches",
       g[100][1] == av and g[101][1] == av and g[103][1] == "true" and g[102][1] == "true")
    m = {"blocking_groups": 300, "blocking_rows": 301, "trim_groups": 302, "trim_rows": 303, "near_groups": 304,
         "near_rows": 305, "padded": 310, "nbsp": 312, "fmt": 313}
    got = {k: int(g[v][1]) for k, v in m.items()}
    ok(f"{db}: email preflight counts == seeded truth", got == exp, json.dumps(got))
    ok(f"{db}: transaction_read_only=on, repeatable read, timeouts 15s/2s", g[900][1] == "on"
       and g[901][1] == "repeatable read" and g[902][1].startswith("15s / 2s"))
    ok(f"{db}: no transaction id assigned", g[909][1] == "")
    # write attempts inside the script's own transaction header
    header = main_sql[:main_sql.index("-- A1-SELECT-BEGIN")]
    for stmt in ("INSERT INTO rides (driver_id, origin, destination, departure_time, available_seats, "
                 "fare_per_seat, status) SELECT 2, 'x', 'y', now(), 1, 1, 'active'",
                 "UPDATE users SET name = name WHERE id = 1", "DELETE FROM bookings WHERE id = 1",
                 "CREATE TABLE a1_should_not_exist (id int)"):
        w = psql(db, sql=None, check=False, *["-c", header + stmt + ";"])
        ok(f"{db}: write rejected in A1 transaction: {stmt.split()[0]} {stmt.split()[1]}",
           w.returncode != 0 and "read-only transaction" in w.stderr)
    after_stats, after_dump = stats(db), dump(db)
    ok(f"{db}: pg_stat_database tuples unchanged", before_stats == after_stats, f"{before_stats} -> {after_stats}")
    ok(f"{db}: pg_dump identical before/after ({len(before_dump)} bytes)", before_dump == after_dump)
    Path(f"/tmp/{db}_a1_output.tsv").write_text(r.stdout)

failed = [n for n, c in results if not c]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
