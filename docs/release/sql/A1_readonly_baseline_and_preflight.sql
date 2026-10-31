-- =====================================================================================================
-- RideShare A1 — read-only production baseline + email preflight (rev 2 logic, aggregates only)
-- File: docs/release/sql/A1_readonly_baseline_and_preflight.sql      Instructions: docs/release/A1_EXECUTION_INSTRUCTIONS.md
--
-- WHAT IT DOES:  reads alembic_version, a schema fingerprint (catalog only), aggregate row counts and the
--                AST-005 email-preflight counts. ONE result grid: (ord, section, check_name, value).
-- WHAT IT NEVER DOES: write, lock for writing, or return an email, name, phone, CNIC, password hash,
--                token, or any free text from user rows. Only counts, booleans, revision ids, index/
--                constraint names and server settings are returned. (Optional integer user ids: see the
--                commented block at the very end; leave it commented unless Hark asks for it.)
-- SAFETY:        the main query is a single SELECT (no INSERT/UPDATE/DELETE/DDL, no volatile writer
--                functions). It is additionally wrapped in a READ ONLY, REPEATABLE READ transaction with
--                short timeouts and ends in ROLLBACK. If an editor runs each statement separately, the
--                SET LOCAL lines only print a warning and the SELECT is still one consistent snapshot.
-- PROOF IN OUTPUT: rows E.* report transaction_read_only, isolation, timeouts, whether this role could
--                write, and txid_current_if_assigned() (empty = this transaction wrote nothing).
-- Compatible: PostgreSQL 12+ (Neon runs 14-17). Safe at revision d924a6e38f10, at head d5e1a0b00002,
--                and anything in between: tables/columns that may not exist are probed, never named
--                directly.
-- =====================================================================================================

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SET LOCAL statement_timeout = '15s';                      -- the SELECT is cancelled after 15 s
SET LOCAL lock_timeout = '2s';                            -- never wait behind DDL / a migration
SET LOCAL idle_in_transaction_session_timeout = '60s';    -- an abandoned tab cannot hold the snapshot
SET LOCAL default_transaction_read_only = on;             -- belt and braces

-- A1-SELECT-BEGIN (the single-statement variant is exactly the text between these markers)
WITH
p AS (   -- Python str.isspace() set (the API trims with str.strip()); invisible format chars
  SELECT '[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]'::text AS ws,
         '[\u200b-\u200d\u2060\ufeff]'::text AS fmt
),
idx AS (SELECT c.relname::text AS name, i.indisvalid AS valid, i.indisready AS ready
        FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relnamespace = to_regnamespace(current_schema())),
col AS (SELECT table_name::text AS t, column_name::text AS c, is_nullable::text AS nullable
        FROM information_schema.columns WHERE table_schema = current_schema()),
con AS (SELECT conname::text AS name, convalidated AS validated
        FROM pg_constraint WHERE connamespace = to_regnamespace(current_schema())),
enm AS (SELECT t.typname::text AS typ, e.enumlabel::text AS lbl
        FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typnamespace = to_regnamespace(current_schema())),
trg AS (SELECT tgname::text AS name FROM pg_trigger WHERE NOT tgisinternal
          AND tgrelid = coalesce(to_regclass('admin_audit_log'), 0::oid)),
-- One marker per migration, in chain order. A marker is TRUE when that migration's objects exist.
mk AS (
  SELECT ARRAY[
    '50872040220b','7c2e9a4b6d10','b83d12f7a906','d924a6e38f10','e3a7c41b9f52','f6b2d8e0c413',
    'c1a7e0b00001','c1a7e0b00002','c1a7e0b00003','d5e1a0b00001','d5e1a0b00002']::text[] AS rev,
  ARRAY[
    to_regclass('users') IS NOT NULL AND to_regclass('rides') IS NOT NULL AND to_regclass('bookings') IS NOT NULL,
    EXISTS (SELECT 1 FROM idx WHERE name = 'uq_bookings_active_passenger_ride'),
    EXISTS (SELECT 1 FROM idx WHERE name = 'ix_rides_status_departure_time_id'),
    NOT EXISTS (SELECT 1 FROM idx WHERE name IN ('ix_users_id','ix_rides_id','ix_bookings_id')),
    (SELECT count(*) FROM idx WHERE name IN ('ix_rides_driver_id_departure_time_id','ix_bookings_passenger_id_id','ix_bookings_ride_id_id')) = 3,
    EXISTS (SELECT 1 FROM idx WHERE name = 'uq_users_email_lower'),
    to_regclass('idempotency_keys') IS NOT NULL,
    EXISTS (SELECT 1 FROM col WHERE t = 'bookings' AND c = 'expires_at'),
    EXISTS (SELECT 1 FROM col WHERE t = 'bookings' AND c = 'seats_requested')
      AND EXISTS (SELECT 1 FROM con WHERE name = 'ck_bookings_seats_requested_range'),
    EXISTS (SELECT 1 FROM col WHERE t = 'users' AND c = 'token_version')
      AND EXISTS (SELECT 1 FROM col WHERE t = 'users' AND c = 'account_status'),
    to_regclass('admin_audit_log') IS NOT NULL]::boolean[] AS ok
),
fp AS (
  SELECT rev, ok, coalesce(array_position(ok, false), 12) AS first_missing FROM mk
),
-- Users: emails are only ever inspected inside aggregates; nothing per-row leaves this CTE chain.
u AS (
  SELECT id, email, role::text AS role,
         to_jsonb(x) ->> 'account_status' AS account_status,       -- NULL when the column is absent
         (to_jsonb(x) ->> 'token_version')::int AS token_version,
         lower(email) AS k_lower,
         lower(regexp_replace(email, '^' || p.ws || '+|' || p.ws || '+$', '', 'g')) AS k_trim,
         lower(regexp_replace(email, p.ws || '|' || p.fmt, '', 'g')) AS k_near,
         email ~ ('^' || p.ws || '|' || p.ws || '$') AS padded,
         email ~ p.ws AS any_ws, email ~ p.fmt AS fmt_chars
  FROM users x, p
),
g_lower AS (SELECT count(*) AS n FROM u GROUP BY k_lower HAVING count(*) > 1),
g_trim  AS (SELECT count(*) AS n FROM u GROUP BY k_trim  HAVING count(*) > 1),
g_near  AS (SELECT count(*) AS n FROM u GROUP BY k_near  HAVING count(*) > 1),
b AS (
  SELECT x.status::text AS status, r.departure_time < now() AS ride_departed, r.status::text AS ride_status,
         coalesce((to_jsonb(x) ->> 'seats_requested')::int, 1) AS seats,
         to_jsonb(x) ? 'seats_requested' AS has_seats_col,
         (to_jsonb(x) ->> 'expires_at')::timestamptz AS expires_at
  FROM bookings x JOIN rides r ON r.id = x.ride_id
),
rows(ord, section, check_name, value) AS (
  -- ---------- A. revision -------------------------------------------------------------------------
  SELECT 100, 'A revision', 'alembic_version.version_num',
         CASE WHEN to_regclass('alembic_version') IS NULL THEN '(table alembic_version missing)'
              ELSE (xpath('//v/text()', query_to_xml(
                     'SELECT string_agg(version_num, '','' ORDER BY version_num) AS v FROM alembic_version',
                     false, true, '')))[1]::text END
  UNION ALL SELECT 101, 'A revision', 'fingerprint_inferred_revision',
         CASE WHEN first_missing = 1 THEN '(none: base tables missing)' ELSE rev[first_missing - 1] END FROM fp
  UNION ALL SELECT 102, 'A revision', 'fingerprint_contiguous (no later marker present)',
         (NOT coalesce(true = ANY (ok[first_missing:11]), false))::text FROM fp
  UNION ALL SELECT 103, 'A revision', 'version_matches_fingerprint',
         (CASE WHEN to_regclass('alembic_version') IS NULL THEN false
               ELSE (xpath('//v/text()', query_to_xml('SELECT string_agg(version_num, '','') AS v FROM alembic_version', false, true, '')))[1]::text
                    = (SELECT CASE WHEN first_missing = 1 THEN '' ELSE rev[first_missing - 1] END FROM fp) END)::text
  UNION ALL SELECT 110 + i, 'B fingerprint', 'marker ' || lpad(i::text, 2, '0') || ' ' || rev[i], ok[i]::text
         FROM fp, generate_series(1, 11) AS i
  -- ---------- B. schema details that matter for the remaining migrations ----------------------------
  UNION ALL SELECT 130, 'B fingerprint', 'enum bookingstatus labels',
         (SELECT string_agg(lbl, ',' ORDER BY lbl) FROM enm WHERE typ = 'bookingstatus')
  UNION ALL SELECT 131, 'B fingerprint', 'enum ridestatus labels',
         (SELECT string_agg(lbl, ',' ORDER BY lbl) FROM enm WHERE typ = 'ridestatus')
  UNION ALL SELECT 132, 'B fingerprint', 'enum userrole labels',
         (SELECT string_agg(lbl, ',' ORDER BY lbl) FROM enm WHERE typ = 'userrole')
  UNION ALL SELECT 133, 'B fingerprint', 'rides.status / bookings.status nullable',
         (SELECT string_agg(t || '=' || nullable, ',' ORDER BY t) FROM col WHERE c = 'status' AND t IN ('rides','bookings'))
  UNION ALL SELECT 134, 'B fingerprint', 'users columns (names only)',
         (SELECT string_agg(c, ',' ORDER BY c) FROM col WHERE t = 'users')
  UNION ALL SELECT 135, 'B fingerprint', 'bookings columns (names only)',
         (SELECT string_agg(c, ',' ORDER BY c) FROM col WHERE t = 'bookings')
  UNION ALL SELECT 136, 'B fingerprint', 'INVALID or not-ready indexes (any; should be empty)',
         coalesce((SELECT string_agg(name, ',' ORDER BY name) FROM idx WHERE NOT valid OR NOT ready), '')
  UNION ALL SELECT 137, 'B fingerprint', 'concurrent-built indexes present/valid',
         (SELECT string_agg(n || '=' || coalesce((SELECT valid::text FROM idx WHERE name = n), 'absent'), ',' ORDER BY n)
            FROM unnest(ARRAY['ix_rides_driver_id_departure_time_id','ix_bookings_passenger_id_id',
                              'ix_bookings_ride_id_id','ix_bookings_pending_expires_at']) AS n)
  UNION ALL SELECT 138, 'B fingerprint', 'email indexes present/valid',
         (SELECT string_agg(n || '=' || coalesce((SELECT valid::text FROM idx WHERE name = n), 'absent'), ',' ORDER BY n)
            FROM unnest(ARRAY['ix_users_email','uq_users_email_lower']) AS n)
  UNION ALL SELECT 139, 'B fingerprint', 'check constraints present/validated',
         (SELECT string_agg(n || '=' || coalesce((SELECT validated::text FROM con WHERE name = n), 'absent'), ',' ORDER BY n)
            FROM unnest(ARRAY['ck_bookings_seats_requested_range','ck_users_account_status',
                              'ck_users_token_version_nonnegative']) AS n)
  UNION ALL SELECT 140, 'B fingerprint', 'admin_audit_log triggers',
         coalesce((SELECT string_agg(name, ',' ORDER BY name) FROM trg), '')
  UNION ALL SELECT 141, 'B fingerprint', 'tables in schema (names only)',
         (SELECT string_agg(tablename::text, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = current_schema())
  -- ---------- C. aggregate row counts (counts only) --------------------------------------------------
  UNION ALL SELECT 200, 'C counts', 'users total', (SELECT count(*)::text FROM u)
  UNION ALL SELECT 201, 'C counts', 'users by role',
         (SELECT string_agg(role || '=' || n, ',' ORDER BY role) FROM (SELECT role, count(*) n FROM u GROUP BY role) s)
  UNION ALL SELECT 202, 'C counts', 'users by account_status',
         coalesce((SELECT string_agg(coalesce(account_status, '(column absent)') || '=' || n, ',' ORDER BY account_status)
                   FROM (SELECT account_status, count(*) n FROM u GROUP BY account_status) s), '')
  UNION ALL SELECT 203, 'C counts', 'users with token_version > 0', (SELECT count(*)::text FROM u WHERE token_version > 0)
  UNION ALL SELECT 210, 'C counts', 'rides by status',
         coalesce((SELECT string_agg(st || '=' || n, ',' ORDER BY st) FROM (SELECT status::text st, count(*) n FROM rides GROUP BY 1) s), '')
  UNION ALL SELECT 211, 'C counts', 'active rides already departed (stale)',
         (SELECT count(*)::text FROM rides WHERE status::text = 'active' AND departure_time < now())
  UNION ALL SELECT 220, 'C counts', 'bookings by status',
         coalesce((SELECT string_agg(status || '=' || n, ',' ORDER BY status) FROM (SELECT status, count(*) n FROM b GROUP BY 1) s), '')
  UNION ALL SELECT 221, 'C counts', 'pending bookings on departed rides (first expiry sweep size)',
         (SELECT count(*)::text FROM b WHERE status = 'pending' AND ride_departed)
  UNION ALL SELECT 222, 'C counts', 'pending bookings with expires_at NULL (legacy TTL rows)',
         CASE WHEN NOT EXISTS (SELECT 1 FROM col WHERE t = 'bookings' AND c = 'expires_at') THEN '(column absent)'
              ELSE (SELECT count(*)::text FROM b WHERE status = 'pending' AND expires_at IS NULL) END
  UNION ALL SELECT 223, 'C counts', 'bookings with seats > 1 (blocks c1a7e0b00003 downgrade)',
         (SELECT count(*)::text FROM b WHERE seats > 1)
  UNION ALL SELECT 224, 'C counts', 'seats booked on pending+accepted bookings',
         (SELECT coalesce(sum(seats), 0)::text FROM b WHERE status IN ('pending','accepted'))
  UNION ALL SELECT 230, 'C counts', 'idempotency_keys rows',
         CASE WHEN to_regclass('idempotency_keys') IS NULL THEN '(table absent)'
              ELSE (xpath('//n/text()', query_to_xml('SELECT count(*) AS n FROM idempotency_keys', false, true, '')))[1]::text END
  UNION ALL SELECT 231, 'C counts', 'admin_audit_log rows',
         CASE WHEN to_regclass('admin_audit_log') IS NULL THEN '(table absent)'
              ELSE (xpath('//n/text()', query_to_xml('SELECT count(*) AS n FROM admin_audit_log', false, true, '')))[1]::text END
  -- ---------- D. email preflight (rev 2 semantics; counts only) -------------------------------------
  UNION ALL SELECT 300, 'D email', 'BLOCKING lower(email) duplicate groups (must be 0 before f6b2d8e0c413)',
         (SELECT count(*)::text FROM g_lower)
  UNION ALL SELECT 301, 'D email', 'rows in blocking groups', (SELECT coalesce(sum(n), 0)::text FROM g_lower)
  UNION ALL SELECT 302, 'D email', 'trim+lower duplicate groups (superset; non-blocking)', (SELECT count(*)::text FROM g_trim)
  UNION ALL SELECT 303, 'D email', 'rows in trim+lower groups', (SELECT coalesce(sum(n), 0)::text FROM g_trim)
  UNION ALL SELECT 304, 'D email', 'near-duplicate groups (all whitespace + invisible chars removed; review only)',
         (SELECT count(*)::text FROM g_near)
  UNION ALL SELECT 305, 'D email', 'rows in near-duplicate groups', (SELECT coalesce(sum(n), 0)::text FROM g_near)
  UNION ALL SELECT 310, 'D email', 'padded rows (leading/trailing Unicode whitespace incl. NBSP; cannot log in)',
         (SELECT count(*)::text FROM u WHERE padded)
  UNION ALL SELECT 311, 'D email', 'rows with any Unicode whitespace', (SELECT count(*)::text FROM u WHERE any_ws)
  UNION ALL SELECT 312, 'D email', 'rows with NBSP (U+00A0)', (SELECT count(*)::text FROM u WHERE email ~ '\u00a0')
  UNION ALL SELECT 313, 'D email', 'rows with invisible format chars (ZWSP/ZWJ/BOM)', (SELECT count(*)::text FROM u WHERE fmt_chars)
  UNION ALL SELECT 314, 'D email', 'mixed-case rows (email <> lower(email))', (SELECT count(*)::text FROM u WHERE email <> k_lower)
  UNION ALL SELECT 315, 'D email', 'non-ASCII rows', (SELECT count(*)::text FROM u WHERE email ~ '[^\u0001-\u007f]')
  UNION ALL SELECT 316, 'D email', 'rows without exactly one at-sign', (SELECT count(*)::text FROM u WHERE email !~ '^[^@]+@[^@]+$')
  UNION ALL SELECT 317, 'D email', 'NULL or empty emails', (SELECT count(*)::text FROM u WHERE email IS NULL OR email = '')
  -- ---------- E. session proof ----------------------------------------------------------------------
  UNION ALL SELECT 900, 'E session', 'transaction_read_only (expect on)', current_setting('transaction_read_only')
  UNION ALL SELECT 901, 'E session', 'transaction_isolation (expect repeatable read)', current_setting('transaction_isolation')
  UNION ALL SELECT 902, 'E session', 'statement_timeout / lock_timeout / idle_in_tx_timeout',
         current_setting('statement_timeout') || ' / ' || current_setting('lock_timeout') || ' / '
         || current_setting('idle_in_transaction_session_timeout')
  UNION ALL SELECT 903, 'E session', 'role could write users (INSERT/UPDATE/DELETE privilege)',
         (has_table_privilege('users', 'INSERT') OR has_table_privilege('users', 'UPDATE')
          OR has_table_privilege('users', 'DELETE'))::text
  UNION ALL SELECT 904, 'E session', 'role is superuser', current_setting('is_superuser')
  UNION ALL SELECT 905, 'E session', 'server in recovery (true = read replica compute)', pg_is_in_recovery()::text
  UNION ALL SELECT 906, 'E session', 'server_version', current_setting('server_version')
  UNION ALL SELECT 907, 'E session', 'database collate / ctype',
         (SELECT datcollate || ' / ' || datctype FROM pg_database WHERE datname = current_database())
  UNION ALL SELECT 908, 'E session', 'users.email collation',
         coalesce((SELECT collname::text FROM pg_collation c JOIN pg_attribute a ON a.attcollation = c.oid
                   WHERE a.attrelid = 'users'::regclass AND a.attname = 'email'), '')
  UNION ALL SELECT 909, 'E session', 'transaction id assigned (expect empty = nothing written)',
         coalesce(txid_current_if_assigned()::text, '')
  UNION ALL SELECT 910, 'E session', 'snapshot taken at (UTC)', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
  UNION ALL SELECT 911, 'E session', 'script', 'A1 v1 (docs/rc-readiness)'
)
SELECT ord, section, check_name, coalesce(value, '') AS value FROM rows ORDER BY ord;
-- A1-SELECT-END

ROLLBACK;

-- -----------------------------------------------------------------------------------------------------
-- OPTIONAL (leave commented unless Hark asks): integer user ids of the BLOCKING groups only.
-- Returns ids, never emails. Run it separately, after the script above, by removing the leading "-- ".
-- BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = '15s'; SET LOCAL lock_timeout = '2s';
-- SELECT array_agg(id ORDER BY id) AS user_ids FROM users GROUP BY lower(email) HAVING count(*) > 1 ORDER BY min(id) LIMIT 200;
-- ROLLBACK;
