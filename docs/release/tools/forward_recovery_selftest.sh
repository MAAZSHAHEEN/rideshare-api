#!/usr/bin/env bash
# Local self-test of FORWARD_RECOVERY_PLAN.md detection queries, forward fixes and backup validation.
# Disposable LOCAL databases only (rs_a1_fr*), synthetic data from seed_synthetic.py. Never point at Neon.
#   export PGPASSWORD=...  ALEMBIC_DIR=/path/to/repo-at-RC  VENV=/path/to/.venv
#   bash docs/release/tools/forward_recovery_selftest.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"            # docs/release
SQL="$HERE/sql"; TOOLS="$HERE/tools"
H=localhost; U=rideshare_ci
: "${ALEMBIC_DIR:?repo checkout with the RC migrations}"; : "${VENV:?python venv with alembic}"
PSQL="psql -h $H -U $U -X -q -v ON_ERROR_STOP=1"
pass=0; fail=0
ok(){ if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }
mkdb(){ $PSQL -d rideshare_ci -c "DROP DATABASE IF EXISTS $1" -c "CREATE DATABASE $1" 2>/dev/null; }
alem(){ (cd "$ALEMBIC_DIR" && DATABASE_URL="postgresql+asyncpg://$U:$PGPASSWORD@$H:5432/$1" "$VENV/bin/alembic" "${@:2}" 2>&1); }
val(){ $PSQL -d "$1" -At -F'|' -f "$SQL/forward_recovery_detect.sql" | awk -F'|' -v k="$2" '$2==k{print $3}'; }
TMP=$(mktemp -d)

# ---------- 1. INVALID concurrent index: simulate, detect, fix ---------------------------------------------
DB=rs_a1_fr_idx; mkdb $DB; alem $DB upgrade head >/dev/null
python3 "$TOOLS/seed_synthetic.py" --target head --users 200 2>/dev/null | $PSQL -d $DB -f - >/dev/null
$PSQL -d $DB -c "DROP INDEX ix_bookings_pending_expires_at"
# A session holding an old snapshot makes CREATE INDEX CONCURRENTLY wait after it has created the (invalid)
# catalog entry; cancelling the build then leaves the index INVALID — exactly an interrupted migration.
( $PSQL -d $DB -c "BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM rides; SELECT pg_sleep(8); COMMIT;" >/dev/null ) &
HOLD=$!; sleep 1
( $PSQL -d $DB -c "CREATE INDEX CONCURRENTLY ix_bookings_pending_expires_at ON bookings (expires_at) WHERE status = 'pending'" >"$TMP/cic.log" 2>&1 ) &
CIC=$!; sleep 2
$PSQL -d rideshare_ci -At -c "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE datname='$DB' AND query LIKE 'CREATE INDEX CONCURRENTLY%'" >/dev/null
wait $CIC; wait $HOLD
ok "interrupted CIC reported cancel" "grep -q 'canceling statement' $TMP/cic.log"
ok "detect: invalid index listed" "[ \"\$(val $DB invalid_or_unready_indexes)\" = 'bookings.ix_bookings_pending_expires_at' ]"
ok "alembic still says head (upgrade would NOT rebuild it)" "[ \"\$(val $DB alembic_version)\" = d5e1a0b00002 ]"
$PSQL -d $DB -f "$SQL/forward_fix_indexes.psql" >"$TMP/fix1.log" 2>&1; rc=$?
ok "forward_fix_indexes.psql exit 0" "[ $rc = 0 ]"
ok "detect after fix: no invalid index" "[ -z \"\$(val $DB invalid_or_unready_indexes)\" ]"
ok "rebuilt index definition matches migration" "$PSQL -d $DB -At -c \"select pg_get_indexdef('ix_bookings_pending_expires_at'::regclass)\" | grep -q \"WHERE (status = 'pending'::bookingstatus)\""
$PSQL -d $DB -f "$SQL/forward_fix_indexes.psql" >/dev/null 2>&1; ok "forward fix idempotent (rerun exit 0)" "[ $? = 0 ]"
# missing (not invalid) index is recreated too
$PSQL -d $DB -c "DROP INDEX ix_bookings_ride_id_id"
ok "detect: missing index listed" "[ \"\$(val $DB expected_indexes_missing_for_revision)\" = ix_bookings_ride_id_id ]"
$PSQL -d $DB -f "$SQL/forward_fix_indexes.psql" >/dev/null 2>&1
ok "missing index recreated" "[ -z \"\$(val $DB expected_indexes_missing_for_revision)\" ]"

# ---------- 2. f6b2 boundary: upgrade blocked by case twins, detect, resolve, continue ---------------------
DB=rs_a1_fr_email; mkdb $DB; alem $DB upgrade d924a6e38f10 >/dev/null
python3 "$TOOLS/seed_synthetic.py" --target d924 --users 200 --email-anomalies 2>/dev/null | $PSQL -d $DB -f - >/dev/null
alem $DB upgrade head >"$TMP/up.log"
ok "upgrade aborts at f6b2 with CaseDuplicateEmails (ids only)" "grep -q CaseDuplicateEmails $TMP/up.log && ! grep -q '@' $TMP/up.log"
ok "detect: stuck at e3a7c41b9f52, 2 twin groups" "[ \"\$(val $DB alembic_version)\" = e3a7c41b9f52 ] && [ \"\$(val $DB 'lower(email) duplicate groups')\" = 2 ]"
# Resolution is a per-account human decision; the synthetic stand-in renames the newer twin.
$PSQL -d $DB -c "UPDATE users u SET email = 'resolved+' || u.id || '@example.com' WHERE id IN (SELECT max(id) FROM users GROUP BY lower(email) HAVING count(*) > 1)"
alem $DB upgrade head >"$TMP/up2.log"
ok "after resolving twins upgrade reaches head" "[ \"\$(val $DB alembic_version)\" = d5e1a0b00002 ] && [ -z \"\$(val $DB invalid_or_unready_indexes)\" ]"

# ---------- 3. Export -> emergency downgrade -> re-upgrade -> forward re-apply ----------------------------
DB=rs_a1_fr_rt; mkdb $DB; alem $DB upgrade head >/dev/null
# 1-seat only so that the c1a7e0b00003 downgrade is allowed (it refuses otherwise: tested in #27).
python3 "$TOOLS/seed_synthetic.py" --target head --users 200 2>/dev/null | $PSQL -d $DB -f - >/dev/null
$PSQL -d $DB -c "UPDATE bookings SET seats_requested = 1"
before=$($PSQL -d $DB -At -c "select (select count(*) from bookings where status='expired')||'/'||(select count(*) from users where account_status='suspended')||'/'||(select count(*) from admin_audit_log)||'/'||(select md5(string_agg(id||':'||actor_id||':'||action||':'||target_id||':'||metadata::text||':'||created_at, ',' order by id)) from admin_audit_log)")
(cd "$TMP" && mkdir -p exp && cd exp && umask 077 && $PSQL -d $DB -f "$SQL/pre_migration_export.psql" >/dev/null) ; rc=$?
ok "pre_migration_export.psql exit 0, files written" "[ $rc = 0 ] && [ -s $TMP/exp/admin_audit_log.csv ] && [ -s $TMP/exp/user_status.csv ] && [ -s $TMP/exp/expired_booking_ids.csv ]"
ok "export files contain no email/phone/CNIC" "! grep -lE '@|\\+92|[0-9]{13}' $TMP/exp/*.csv"
alem $DB downgrade c1a7e0b00001 >"$TMP/down.log"
ok "downgrade to c1a7e0b00001: expired gone, audit table gone" "[ \"\$(val $DB alembic_version)\" = c1a7e0b00001 ] && [ \"\$(val $DB 'admin_audit_log rows')\" = '(table absent)' ] && [ \"\$(val $DB bookings_status_expired)\" = 0 ]"
ok "detect: enum label 'expired' left behind is reported" "[ \"\$(val $DB 'enum_expired_label_without_expires_at_column (downgraded c1a7e0b00002)')\" = true ]"
alem $DB upgrade head >/dev/null
(cd "$TMP/exp" && $PSQL -d $DB -f "$SQL/reapply_after_downgrade.psql" >"$TMP/reapply.log" 2>&1); rc=$?
after=$($PSQL -d $DB -At -c "select (select count(*) from bookings where status='expired')||'/'||(select count(*) from users where account_status='suspended')||'/'||(select count(*) from admin_audit_log)||'/'||(select md5(string_agg(id||':'||actor_id||':'||action||':'||target_id||':'||metadata::text||':'||created_at, ',' order by id)) from admin_audit_log)")
ok "re-apply exit 0; expired/suspended/audit restored exactly ($before)" "[ $rc = 0 ] && [ \"$before\" = \"$after\" ]"
ok "token_version bumped past exported values" "[ \$($PSQL -d $DB -At -c \"select count(*) from users where account_status='suspended' and token_version < 2\") = 0 ]"
ok "audit log still append-only after re-import" "! $PSQL -d $DB -c 'UPDATE admin_audit_log SET reason = reason' >/dev/null 2>&1 || [ \$($PSQL -d $DB -At -c 'select rolsuper from pg_roles where rolname=current_user') = t ]"
(cd "$TMP/exp" && $PSQL -d $DB -f "$SQL/reapply_after_downgrade.psql" >/dev/null 2>&1); ok "re-apply rerun refused (no double insert)" "[ $? != 0 ]"

# ---------- 4. Backup validation: pg_dump of affected tables -> restore to scratch -> restore_verify diff --
SRC=rs_a1_head_fr_src; DST=rs_a1_fr_restore; mkdb $SRC; alem $SRC upgrade head >/dev/null
python3 "$TOOLS/seed_synthetic.py" --target head --users 200 2>/dev/null | $PSQL -d $SRC -f - >/dev/null
pg_dump -h $H -U $U -d $SRC -Fc --no-owner -f "$TMP/full.dump"
ok "custom-format dump lists all 6 tables" "[ \$(pg_restore -l $TMP/full.dump | grep -c 'TABLE DATA public') = 6 ]"
mkdb $DST; pg_restore -h $H -U $U -d $DST --no-owner --exit-on-error "$TMP/full.dump"; ok "pg_restore exit 0" "[ $? = 0 ]"
$PSQL -d $SRC -At -f "$SQL/restore_verify.sql" >"$TMP/v_src.txt"; $PSQL -d $DST -At -f "$SQL/restore_verify.sql" >"$TMP/v_dst.txt"
ok "restore_verify.sql identical on source and restore" "diff -q $TMP/v_src.txt $TMP/v_dst.txt >/dev/null"
pg_dump -h $H -U $U -d $SRC -Fc --no-owner -t users -t bookings -t admin_audit_log -f "$TMP/tables.dump"
ok "per-table dump (users,bookings,admin_audit_log) readable" "[ \$(pg_restore -l $TMP/tables.dump | grep -c 'TABLE DATA public') = 3 ]"
# A restore that drops a row must be caught by the verify diff.
$PSQL -d $DST -c "ALTER TABLE bookings DISABLE TRIGGER ALL; DELETE FROM bookings WHERE id = (SELECT max(id) FROM bookings);" >/dev/null
$PSQL -d $DST -At -f "$SQL/restore_verify.sql" >"$TMP/v_dst2.txt"
ok "verify diff catches a missing booking (negative control)" "! diff -q $TMP/v_src.txt $TMP/v_dst2.txt >/dev/null"

for d in rs_a1_fr_idx rs_a1_fr_email rs_a1_fr_rt rs_a1_head_fr_src rs_a1_fr_restore; do $PSQL -d rideshare_ci -c "DROP DATABASE IF EXISTS $d" 2>/dev/null; done
rm -rf "$TMP"
echo; echo "$pass passed, $fail failed"; [ $fail = 0 ]
