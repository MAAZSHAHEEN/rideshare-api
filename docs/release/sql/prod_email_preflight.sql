-- RideShare AST-005 production preflight, revision 2 (reviewed: docs/release/PREFLIGHT_REVIEW.md).
-- READ-ONLY. Returns aggregate counts and user IDs only; no email address, name, phone or CNIC is
-- selected or returned. Changes nothing: the transaction is READ ONLY and ends in ROLLBACK.
--
-- Run (approval-gated; Maaz only), preferably with a read-only role and psql:
--   PGOPTIONS='-c default_transaction_read_only=on' \
--   psql "$READONLY_DIRECT_URL" -X -v ON_ERROR_STOP=1 -f prod_email_preflight.sql
-- -X: ignore ~/.psqlrc. ON_ERROR_STOP: on any error psql exits and the server rolls the
-- transaction back. In the Neon SQL Editor, paste the whole file and run it as one batch.
--
-- Whitespace: the API trims input with Python str.strip(), which removes every Unicode
-- whitespace character. PostgreSQL's \s depends on the database ctype (glibc does not treat
-- U+00A0 as space, for example), so this file uses an explicit class equal to Python's
-- str.isspace() set instead of \s.
--   ASCII 09-0D, 1C-1F, 20; U+0085, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F,
--   U+205F, U+3000.

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;  -- one snapshot for every query
SET LOCAL statement_timeout = '15s';                     -- this transaction only
SET LOCAL lock_timeout = '3s';                           -- never queue behind DDL on users
SET LOCAL idle_in_transaction_session_timeout = '60s';   -- an abandoned editor tab cannot hold a snapshot

-- 1. Proof the transaction is read-only (must be 'on'), plus facts that affect lower().
SELECT current_setting('transaction_read_only') AS read_only,
       current_setting('server_version')        AS pg_version,
       datcollate, datctype,
       (SELECT collname FROM pg_collation c JOIN pg_attribute a ON a.attcollation = c.oid
         WHERE a.attrelid = 'users'::regclass AND a.attname = 'email') AS email_column_collation,
       (SELECT pg_get_indexdef(indexrelid) FROM pg_index
         WHERE indexrelid = to_regclass('uq_users_email_lower'))         AS existing_uq_index
FROM pg_database WHERE datname = current_database();

-- 2. Counts by category.
SELECT
  count(*)                                                                                         AS total_users,
  count(*) FILTER (WHERE email ~ '^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$') AS padded_whitespace,
  count(*) FILTER (WHERE email ~ '[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]')   AS any_whitespace,
  count(*) FILTER (WHERE email ~ '[\u200b-\u200d\u2060\ufeff]')                                    AS invisible_format_chars,
  count(*) FILTER (WHERE email <> lower(email))                                                    AS mixed_case,
  count(*) FILTER (WHERE email ~ '[^\u0001-\u007f]')                                               AS non_ascii,
  count(*) FILTER (WHERE email !~ '^[^@]+@[^@]+$')                                                 AS not_single_at,
  count(*) FILTER (WHERE email IS NULL OR email = '')                                              AS null_or_empty
FROM users;

-- 3. Groups that BLOCK migration f6b2d8e0c413: equal under lower(email), the exact expression of
--    uq_users_email_lower and of the migration's own check. First 200 groups only.
SELECT (SELECT count(*) FROM (SELECT 1 FROM users GROUP BY lower(email) HAVING count(*) > 1) n)
         AS case_duplicate_groups,
       coalesce(json_agg(ids ORDER BY ids[1]), '[]') AS user_ids_first_200_groups
FROM (SELECT array_agg(id ORDER BY id) AS ids FROM users
      GROUP BY lower(email) HAVING count(*) > 1 ORDER BY min(id) LIMIT 200) g;

-- 4. Groups equal after the API's trim + lower (same person, different stored spelling). They do
--    NOT block the migration, but the padded member can never log in and registration will not
--    see it as a twin. Superset of section 3. First 200 groups only.
WITH t AS (
  SELECT id, lower(regexp_replace(email,
           '^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$',
           '', 'g')) AS k
  FROM users)
SELECT (SELECT count(*) FROM (SELECT 1 FROM t GROUP BY k HAVING count(*) > 1) n) AS trim_case_duplicate_groups,
       coalesce(json_agg(ids ORDER BY ids[1]), '[]') AS user_ids_first_200_groups
FROM (SELECT array_agg(id ORDER BY id) AS ids FROM t GROUP BY k HAVING count(*) > 1
      ORDER BY min(id) LIMIT 200) g;

-- 5. IDs of padded rows (cannot log in: input is always trimmed). At most 100.
SELECT coalesce(json_agg(id ORDER BY id), '[]') AS padded_user_ids_first_100
FROM (SELECT id FROM users
      WHERE email ~ '^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$'
      ORDER BY id LIMIT 100) p;

ROLLBACK;
