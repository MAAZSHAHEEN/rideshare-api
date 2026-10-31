-- RideShare staging sanitization. Run ONCE, immediately after creating a Neon branch that copies
-- production data, BEFORE any application, preview deploy or person other than the operator
-- connects. NEVER run against production: the guard below refuses unless the operator has set
-- the session variable to the branch name *and* the database name contains no 'prod'.
--
--   psql "$BRANCH_DIRECT_URL" -X -v ON_ERROR_STOP=1 \
--        -c "SET rideshare.sanitize_branch = 'staging-rc-YYYYMMDD'" -f staging_sanitize.sql
--   (psql runs -c and -f in one session only with a single invocation like this; or put the SET
--    line at the top of a copy of this file.)
--
-- Every user ends with: email user<id>@example.com, name "User <id>", phone +92000<id 7 digits>,
-- CNIC 00000-<id 7 digits>-0, an unusable password hash (login always fails cleanly; staging
-- test accounts are registered fresh through the API), account_status/token_version kept.
-- Idempotency replay bodies are deleted (they echo booking payloads). The audit log carries
-- no PII by design (models.AdminAuditLog) and is append-only, so it is left as is.

\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DO $$
BEGIN
  IF coalesce(current_setting('rideshare.sanitize_branch', true), '') = '' THEN
    RAISE EXCEPTION 'refusing: SET rideshare.sanitize_branch = ''<neon branch name>'' first';
  END IF;
  IF current_database() ILIKE '%prod%' THEN
    RAISE EXCEPTION 'refusing: database name looks like production';
  END IF;
END $$;

LOCK TABLE users IN ACCESS EXCLUSIVE MODE;  -- nothing reads half-sanitized rows

UPDATE users SET
  email        = 'user' || id || '@example.com',
  name         = 'User ' || id,
  phone_number = '+92000' || lpad(id::text, 7, '0'),
  cnic         = '00000-' || lpad(id::text, 7, '0') || '-0',
  password     = '!sanitized-no-login';

DO $$
BEGIN
  IF to_regclass('idempotency_keys') IS NOT NULL THEN
    EXECUTE 'DELETE FROM idempotency_keys';
  END IF;
END $$;

-- Verification inside the same transaction: every count must be 0, or the whole thing aborts.
DO $$
DECLARE leftover bigint;
BEGIN
  SELECT count(*) INTO leftover FROM users
   WHERE email   <> 'user' || id || '@example.com'
      OR name    <> 'User ' || id
      OR cnic    !~ '^00000-[0-9]{7}-0$'
      OR phone_number !~ '^\+92000[0-9]{7}$'
      OR password <> '!sanitized-no-login';
  IF leftover <> 0 THEN
    RAISE EXCEPTION 'sanitization incomplete: % user rows', leftover;
  END IF;
END $$;

COMMIT;

-- Aggregate-only report (no PII is left to print anyway).
SELECT count(*) AS users_sanitized,
       count(*) FILTER (WHERE email LIKE 'user%@example.com') AS example_emails,
       (SELECT count(*) FROM users WHERE id > 9999999) AS ids_over_7_digits  -- would need wider lpad
FROM users;
