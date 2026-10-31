-- RideShare forward-recovery DETECTION queries (docs/release/FORWARD_RECOVERY_PLAN.md §3).
-- READ-ONLY, aggregates/object names only, no PII. Run before and after every migration step, and when an
-- upgrade fails. Safe at any revision from d924a6e38f10 to head (missing objects are probed, not named).
--   psql "$URL" -X -v ON_ERROR_STOP=1 -A -F ' | ' -f docs/release/sql/forward_recovery_detect.sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SET LOCAL statement_timeout = '30s';
SET LOCAL lock_timeout = '2s';
WITH
idx AS (SELECT c.relname::text AS name, t.relname::text AS tbl, i.indisvalid AS valid, i.indisready AS ready
        FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid
        WHERE c.relnamespace = to_regnamespace(current_schema())),
col AS (SELECT table_name::text AS t, column_name::text AS c FROM information_schema.columns
        WHERE table_schema = current_schema()),
has AS (SELECT EXISTS (SELECT 1 FROM col WHERE t = 'bookings' AND c = 'seats_requested') AS seats,
               EXISTS (SELECT 1 FROM col WHERE t = 'bookings' AND c = 'expires_at')      AS expires,
               EXISTS (SELECT 1 FROM col WHERE t = 'users'    AND c = 'account_status')  AS status,
               to_regclass('admin_audit_log') IS NOT NULL                                AS audit),
b AS (SELECT x.status::text AS status, r.status::text AS ride_status, r.departure_time,
             (to_jsonb(x) ->> 'seats_requested')::int AS seats, (to_jsonb(x) ->> 'expires_at')::timestamptz AS expires_at
      FROM bookings x JOIN rides r ON r.id = x.ride_id),
rows(ord, check_name, value, expect) AS (
            SELECT 10, 'alembic_version', (xpath('//v/text()', query_to_xml(
                     'SELECT string_agg(version_num, '','') AS v FROM alembic_version', false, true, '')))[1]::text, 'one revision'
  UNION ALL SELECT 20, 'invalid_or_unready_indexes', coalesce((SELECT string_agg(tbl || '.' || name, ',' ORDER BY name)
                     FROM idx WHERE NOT valid OR NOT ready), ''), 'empty'
  UNION ALL SELECT 21, 'reindex_leftovers (_ccnew/_ccold)', coalesce((SELECT string_agg(name, ',') FROM idx
                     WHERE name ~ '_cc(new|old)[0-9]*$'), ''), 'empty'
  UNION ALL SELECT 22, 'expected_indexes_missing_for_revision', coalesce((SELECT string_agg(n, ',') FROM unnest(
                     ARRAY['ix_rides_driver_id_departure_time_id','ix_bookings_passenger_id_id','ix_bookings_ride_id_id']
                     || CASE WHEN (SELECT expires FROM has) THEN ARRAY['ix_bookings_pending_expires_at'] ELSE ARRAY[]::text[] END
                     || CASE WHEN to_regclass('idempotency_keys') IS NOT NULL THEN ARRAY['uq_users_email_lower'] ELSE ARRAY[]::text[] END) n
                     WHERE NOT EXISTS (SELECT 1 FROM idx WHERE name = n)
                       AND EXISTS (SELECT 1 FROM idx WHERE name = 'uq_bookings_active_passenger_ride')
                       AND (SELECT count(*) FROM idx WHERE name IN ('ix_rides_driver_id_departure_time_id','ix_bookings_passenger_id_id','ix_bookings_ride_id_id')) > 0), ''),
                     'empty (only checked once e3a7c41b9f52 started)'
  UNION ALL SELECT 30, 'constraints_not_validated', coalesce((SELECT string_agg(conname::text, ',') FROM pg_constraint
                     WHERE connamespace = to_regnamespace(current_schema()) AND NOT convalidated), ''), 'empty'
  UNION ALL SELECT 40, 'enum_expired_label_without_expires_at_column (downgraded c1a7e0b00002)',
                     (EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
                              WHERE t.typname = 'bookingstatus' AND e.enumlabel = 'expired')
                      AND NOT (SELECT expires FROM has))::text, 'false (true is harmless; note it)'
  UNION ALL SELECT 41, 'bookings_status_expired', (SELECT count(*)::text FROM b WHERE status = 'expired'), 'info'
  UNION ALL SELECT 42, 'pending_past_effective_expiry (expiry job not keeping up)',
                     (SELECT count(*)::text FROM b WHERE status = 'pending'
                        AND least(coalesce(expires_at, departure_time), departure_time) <= now()), '0 when the job runs'
  UNION ALL SELECT 50, 'seat_invariant: rides with available_seats < 0', (SELECT count(*)::text FROM rides WHERE available_seats < 0), '0'
  UNION ALL SELECT 51, 'seat_invariant: seats_requested outside 1..8',
                     CASE WHEN (SELECT seats FROM has) THEN (SELECT count(*)::text FROM b WHERE seats NOT BETWEEN 1 AND 8) ELSE '(column absent)' END, '0'
  UNION ALL SELECT 52, 'bookings with seats_requested > 1 (blocks c1a7e0b00003 downgrade)',
                     CASE WHEN (SELECT seats FROM has) THEN (SELECT count(*)::text FROM b WHERE seats > 1) ELSE '(column absent)' END, 'info'
  UNION ALL SELECT 53, 'live bookings on cancelled/completed rides', (SELECT count(*)::text FROM b
                     WHERE status IN ('pending','accepted') AND ride_status IN ('cancelled','completed')), '0'
  UNION ALL SELECT 54, 'duplicate live booking (passenger, ride)', (SELECT count(*)::text FROM (SELECT 1 FROM bookings
                     WHERE status::text IN ('pending','accepted') GROUP BY ride_id, passenger_id HAVING count(*) > 1) d), '0'
  UNION ALL SELECT 60, 'lower(email) duplicate groups', (SELECT count(*)::text FROM (SELECT 1 FROM users GROUP BY lower(email)
                     HAVING count(*) > 1) d), '0 (else f6b2d8e0c413 aborts)'
  UNION ALL SELECT 70, 'suspended users', CASE WHEN (SELECT status FROM has) THEN (SELECT count(*)::text FROM users
                     WHERE (to_jsonb(users) ->> 'account_status') = 'suspended') ELSE '(column absent)' END, 'compare with export'
  UNION ALL SELECT 71, 'admin_audit_log rows', CASE WHEN (SELECT audit FROM has) THEN (xpath('//n/text()', query_to_xml(
                     'SELECT count(*) AS n FROM admin_audit_log', false, true, '')))[1]::text ELSE '(table absent)' END, 'compare with export'
  UNION ALL SELECT 72, 'admin_audit_log append-only triggers enabled', (SELECT count(*)::text FROM pg_trigger
                     WHERE tgname IN ('admin_audit_log_no_update_delete','admin_audit_log_no_truncate') AND tgenabled <> 'D'), '2 at >= d5e1a0b00002'
  UNION ALL SELECT 80, 'other sessions in a transaction > 60 s (stall CREATE INDEX CONCURRENTLY)',
                     (SELECT count(*)::text FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()
                        AND xact_start < now() - interval '60 seconds'), '0 before upgrading'
  UNION ALL SELECT 81, 'sessions waiting on a lock', (SELECT count(*)::text FROM pg_stat_activity
                     WHERE datname = current_database() AND wait_event_type = 'Lock'), '0'
  UNION ALL SELECT 82, 'CREATE INDEX in progress (pg_stat_progress_create_index)',
                     (SELECT count(*)::text FROM pg_stat_progress_create_index), '0 when idle'
)
SELECT ord, check_name, coalesce(value, '') AS value, expect FROM rows ORDER BY ord;
ROLLBACK;
