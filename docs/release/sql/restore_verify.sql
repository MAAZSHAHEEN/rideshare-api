-- RideShare restore verification. READ-ONLY. Run on the restored branch/database AND on the source
-- (or on the pre-incident branch) and diff the two outputs. Aggregates only, no PII.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SET LOCAL statement_timeout = '60s';

-- 1. Schema revision
SELECT 'alembic_version' AS check, string_agg(version_num, ',') AS value FROM alembic_version;

-- 2. Row counts per table (exact) and newest timestamps (proves how recent the restore point is)
SELECT 'rows.users' AS check, count(*)::text AS value FROM users
UNION ALL SELECT 'rows.rides', count(*)::text FROM rides
UNION ALL SELECT 'rows.bookings', count(*)::text FROM bookings
UNION ALL SELECT 'rows.idempotency_keys', count(*)::text FROM idempotency_keys
UNION ALL SELECT 'rows.admin_audit_log', count(*)::text FROM admin_audit_log
UNION ALL SELECT 'max.users.created_at', coalesce(max(created_at)::text, '-') FROM users
UNION ALL SELECT 'max.bookings.id', coalesce(max(id)::text, '-') FROM bookings
UNION ALL SELECT 'max.admin_audit_log.id', coalesce(max(id)::text, '-') FROM admin_audit_log;

-- 3. Content fingerprints (md5 over ids + non-PII state; equal fingerprints = equal data)
SELECT 'md5.rides' AS check, md5(coalesce(string_agg(id || ':' || status || ':' || available_seats, ',' ORDER BY id), '')) AS value FROM rides
UNION ALL SELECT 'md5.bookings', md5(coalesce(string_agg(id || ':' || ride_id || ':' || passenger_id || ':' || status || ':' || seats_requested, ',' ORDER BY id), '')) FROM bookings
UNION ALL SELECT 'md5.users_state', md5(coalesce(string_agg(id || ':' || role || ':' || account_status || ':' || token_version, ',' ORDER BY id), '')) FROM users;

-- 4. Constraint and index validity
SELECT 'constraints.not_validated' AS check, count(*)::text AS value FROM pg_constraint c
  JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' AND NOT c.convalidated
UNION ALL SELECT 'indexes.invalid', count(*)::text FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND NOT i.indisvalid
UNION ALL SELECT 'indexes.expected_present', count(*)::text FROM pg_class
  WHERE relname IN ('uq_users_email_lower', 'uq_bookings_active_passenger_ride',
                    'ix_rides_driver_id_departure_time_id', 'ix_bookings_passenger_id_id', 'ix_bookings_ride_id_id')
UNION ALL SELECT 'triggers.audit_append_only', count(*)::text FROM pg_trigger
  WHERE tgname IN ('admin_audit_log_no_update_delete', 'admin_audit_log_no_truncate') AND tgenabled <> 'D';

-- 5. Business invariants (each must be 0)
SELECT 'inv.negative_seats' AS check, count(*)::text AS value FROM rides WHERE available_seats < 0
UNION ALL SELECT 'inv.duplicate_live_booking', count(*)::text FROM (
  SELECT 1 FROM bookings WHERE status IN ('pending','accepted') GROUP BY ride_id, passenger_id HAVING count(*) > 1) d
UNION ALL SELECT 'inv.orphan_bookings', count(*)::text FROM bookings b LEFT JOIN rides r ON r.id = b.ride_id WHERE r.id IS NULL
UNION ALL SELECT 'inv.live_on_cancelled_ride', count(*)::text FROM bookings b JOIN rides r ON r.id = b.ride_id
  WHERE r.status = 'cancelled' AND b.status IN ('pending','accepted');

-- 6. Sequences ahead of data (a restore that resets sequences would make the next INSERT fail)
SELECT 'seq.users_ok' AS check, (SELECT last_value FROM users_id_seq) >= coalesce((SELECT max(id) FROM users), 0) AS value
UNION ALL SELECT 'seq.rides_ok', (SELECT last_value FROM rides_id_seq) >= coalesce((SELECT max(id) FROM rides), 0)
UNION ALL SELECT 'seq.bookings_ok', (SELECT last_value FROM bookings_id_seq) >= coalesce((SELECT max(id) FROM bookings), 0);
ROLLBACK;
