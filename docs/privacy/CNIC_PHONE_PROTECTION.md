# CNIC and phone number protection (AST-007)

Status: **design only.** Nothing in this document has been implemented beyond the
AST-007 validation work listed in section 2. No schema change, migration, secret or
environment variable is introduced by AST-007. Every production step in section 6 is
behind Maaz's approval gates (production SQL, migrations, env/secret changes).

## 1. Inventory: where CNIC and phone live today (rideshare-v2 @ e8da425 + AST-007)

| Place | CNIC | Phone | Notes |
|---|---|---|---|
| `models.User.cnic` / `users.cnic` | **plaintext** `VARCHAR`, `UNIQUE` (`users_cnic_key`) | – | Created in `alembic/versions/50872040220b_initial_tables.py`. |
| `models.User.phone_number` / `users.phone_number` | – | **plaintext** `VARCHAR`, not unique, no index | Same migration. |
| Database backups / Neon branches / PITR history | plaintext | plaintext | Provider encrypts storage at rest, but anyone with DB credentials or a dump reads them in clear. |
| Write path `POST /auth/register` (`routers/auth.py`) | received, normalised, stored | received, normalised, stored | The only code that writes either field. |
| Read paths | duplicate check only (`register`) | none | Login is by **email**; no endpoint looks up a user by CNIC or phone. |
| API responses | **not returned** | **not returned** | `UserResponse` (`/auth/register`, `/me`) = id, name, email, role, allowed_modes. `PassengerSummary` (driver booking lists) = id, name. `RideResponse` / booking responses carry no user PII. Covered by `test_registration_responses_exclude_sensitive_fields`, `test_frontend_reads`, `test_input_validation`. |
| JWT | not present | not present | Claims are `sub`, `role`, `iat`, `exp`. |
| 422 validation errors (`validation_errors.py`) | never echoed | never echoed | Allowlisted keys `type/loc/msg`; messages are fixed text; `input`/`ctx` dropped. |
| Pydantic `repr` / tracebacks of `UserCreate` | hidden (`repr=False`) | hidden (`repr=False`) | New in AST-007. |
| Application logs | none | none | The app has no logging calls; tested with root logger at DEBUG during a successful registration. |
| SQLAlchemy exception text / SQL echo | bound parameters hidden | bound parameters hidden | New in AST-007: `create_async_engine(..., hide_parameters=True)` in `database.py`. |
| **Residual: PostgreSQL error DETAIL** | **can appear** | **can appear** | A unique violation's DETAIL is `Key (cnic)=(…) already exists`; NOT NULL / CHECK violations print `Failing row contains (…)` with the whole row. `register` turns CNIC/email unique violations into a 409 without logging, but any *other* `IntegrityError` is re-raised and the server's traceback log will contain DETAIL. Driver-side coercion errors (asyncpg `DataError`) also quote the value. Fix: section 7, item 1. |
| Uvicorn access log | not present (JSON body) | not present | Query strings are logged; only ride search params (`origin`, `destination`, dates) travel in queries. |
| Frontend (`frontend/src/api/types.ts`, `AuthPage.tsx`) | sent once in the register request | sent once | Not stored in browser storage; the session keeps only the token in memory. |

## 2. What AST-007 changed (implemented)

- **CNIC**: accepts `XXXXX-XXXXXXX-X` or `XXXXXXXXXXXXX` (ASCII digits only; Unicode/Arabic-Indic digits rejected), surrounding whitespace trimmed, raw input ≤ 32 chars. **Canonical stored form for new rows: 13 digits, no hyphens.**
- **Phone**: accepts `03XXXXXXXXX`, `+923XXXXXXXXX`, `923XXXXXXXXX`, with optional spaces or hyphens as separators, raw input ≤ 32 chars. **Canonical stored form: E.164 `+923XXXXXXXXX`.** Landlines, `0092…`, and `+920…` are rejected.
- Fixed, value-free 422 messages for both, allowlisted in `validation_errors.py`.
- Duplicate-CNIC check compares digits only (`regexp_replace(cnic, '[^0-9]', '', 'g') = :canonical`), so legacy rows stored as `12345-1234567-1`, with spaces, or untrimmed still block a duplicate registration.

## 3. Legacy normalisation gap (documented, not fixed by a migration)

Rows created before AST-007 were stored **verbatim**, with no validation:

1. `users.cnic` may be hyphenated, spaced, untrimmed, or not a CNIC at all. The unique constraint `users_cnic_key` is on the raw string, so it does **not** stop `12345-1234567-1` and `1234512345671` coexisting. AST-007's digit-normalised duplicate check closes this for every *new* registration. Remaining edge: a legacy value whose digits are not 13 long (garbage) can never match anything.
2. The digit-normalised check is **not index-backed** (sequential scan on `users` per registration). Fine at MVP scale (thousands of rows); the blind index in section 4 replaces it.
3. Concurrency: two simultaneous *new* registrations of the same CNIC both arrive canonical, so `users_cnic_key` still catches the loser (409, existing race test passes). A legacy row is already committed, so the pre-insert check sees it.
4. `users.phone_number` is neither unique nor looked up anywhere, so legacy formats break nothing today. Any future "find by phone", OTP or uniqueness rule **must** first backfill to E.164 (section 6) or compare a normalised form.
5. No `CHECK` constraint was added: legacy production rows would violate it. `NOT VALID` constraints are possible later, after the backfill.

Read-only preflight to size the gap (**reviewed design only; NOT approved to run against production or staging**):

```sql
-- counts only, no values selected
SELECT
  count(*)                                                     AS users,
  count(*) FILTER (WHERE cnic !~ '^[0-9]{13}$')                AS cnic_non_canonical,
  count(*) FILTER (WHERE length(regexp_replace(cnic, '[^0-9]', '', 'g')) <> 13) AS cnic_not_13_digits,
  count(*) FILTER (WHERE phone_number !~ '^\+923[0-9]{9}$')    AS phone_non_canonical
FROM users;

SELECT count(*) AS cnic_digit_duplicate_groups FROM (
  SELECT regexp_replace(cnic, '[^0-9]', '', 'g') FROM users
  GROUP BY 1 HAVING count(*) > 1
) d;
```

## 4. Target design: encryption + blind index

Goal: CNIC and phone are unreadable to anyone holding only a DB dump/backup, while
duplicate detection (CNIC unique; phone lookup later) keeps working with an index.

### 4.1 Columns (expand phase, all nullable, no constraints at first)

| Column | Type | Purpose |
|---|---|---|
| `cnic_ct` | `BYTEA` | AES-256-GCM ciphertext of the canonical CNIC (`nonce‖ciphertext‖tag`). |
| `cnic_bidx` | `BYTEA` (32 bytes) | `HMAC-SHA256(K_bidx_cnic, canonical_cnic)`. Unique (partial, `WHERE cnic_bidx IS NOT NULL`). |
| `phone_ct` | `BYTEA` | AES-256-GCM ciphertext of the E.164 phone. |
| `phone_bidx` | `BYTEA` (32 bytes) | `HMAC-SHA256(K_bidx_phone, e164_phone)`. Plain (non-unique) index. |
| `pii_key_version` | `SMALLINT` | Which encryption key version wrote `*_ct`. |

### 4.2 Cryptography

- Library: `cryptography` (already pinned, 46.0.5) – `AESGCM`. No home-made crypto.
- Encryption: random 96-bit nonce per value; AAD = `b"rideshare/users/<column>/v<key_version>"` so a ciphertext cannot be swapped between columns.
- Blind index: HMAC-SHA256 over the **canonical** form only (13-digit CNIC, E.164 phone) – normalisation from AST-007 is a prerequisite. Separate keys per column and separate from the encryption key, so one leaked key does not expose the others.
- Key material: `PII_ENC_KEYS` (JSON map `version → base64 32-byte key`, plus `PII_ENC_ACTIVE_VERSION`), `PII_BIDX_KEY_CNIC`, `PII_BIDX_KEY_PHONE`, held in the FastAPI Cloud secret store (later a KMS with envelope encryption). Never in the DB, never in the repo, never in logs.
- Low-entropy warning: the CNIC space is ~10¹³. With the HMAC key secret, the index reveals only equality. **If `PII_BIDX_KEY_CNIC` leaks together with the DB, CNICs can be brute-forced** (10¹³ HMACs is feasible on GPUs). Treat the bidx keys as tier-0 secrets; do not truncate the HMAC (collisions would break uniqueness).
- Display: the API never returns CNIC/phone. If an admin view needs it later, return a mask (`*****-*******-1`, `+92 3** *** **67`) and decrypt only behind an admin role with an audit-log entry (M5).

### 4.3 Code shape

- `pii.py`: `encrypt(column, value) -> bytes`, `decrypt(column, blob) -> str`, `bidx(column, canonical) -> bytes`, key loading/validation at startup (fail closed if keys missing when `PII_MODE != plaintext`).
- `PII_MODE` env flag: `plaintext` (today) → `dual` (write both, read plaintext) → `encrypted` (write both, read/lookup via bidx) → `encrypted_only` (stop writing plaintext).
- Duplicate check in `encrypted` mode: `WHERE cnic_bidx = :bidx` (indexed) **OR** `(cnic_bidx IS NULL AND regexp_replace(cnic,'[^0-9]','','g') = :canonical)` until the backfill has no NULLs left.
- Unique-violation mapping in `register` adds the new partial unique index name (e.g. `uq_users_cnic_bidx`) next to `users_cnic_key`.

## 5. Tests the implementation must carry

Round-trip; tamper (flipped byte → decrypt fails); wrong AAD/column swap fails; bidx deterministic and key-separated; key rotation (old version decrypts, new writes); `PII_MODE` transitions; duplicate detection through bidx and through the legacy fallback; migration upgrade → downgrade → upgrade; registration race on the bidx unique index; logs/422 still free of values (reuse `tests/test_input_validation.py` assertions).

## 6. Migration plan (expand → backfill → switch → contract)

Every step that touches staging or production needs Maaz's explicit approval.

0. **Prereqs:** AST-007 merged (canonical input); keys generated and stored as secrets (approval: env/secret change); verified backup + restore rehearsal (M6 ops doc); preflight counts from section 3 (approval: production SQL).
1. **Expand migration** (reversible): add the five nullable columns. Create `uq_users_cnic_bidx` as a partial unique index with `CREATE UNIQUE INDEX CONCURRENTLY` in its own migration (outside a transaction). No `NOT NULL`, no `CHECK`.
2. **Dual write** (`PII_MODE=dual`): new and updated users get plaintext + `*_ct` + `*_bidx`.
3. **Backfill job** (idempotent, resumable by `id`, batches of ~500, short transactions): for each row with `cnic_bidx IS NULL` normalise legacy CNIC (digits only). If it is 13 digits, write ct + bidx. If two legacy rows share the same digits, the unique index rejects the second: **do not auto-merge**; record the user ids (not values) in a report for manual review. Rows that are not 13 digits get `cnic_ct` of the raw value and `cnic_bidx` NULL, and are flagged. Same for phone (E.164 or flagged).
4. **Verify:** counts match; a random sample decrypts to the normalised plaintext; NULL-bidx rows equal the flagged list; full backend suite against a restored staging copy.
5. **Switch reads** (`PII_MODE=encrypted`), soak ≥ 7 days.
6. **Contract** (separate release, irreversible without keys/backups): set `PII_MODE=encrypted_only`, overwrite plaintext with NULL (make columns nullable first), later drop `users_cnic_key` and the plaintext columns. Requires a fresh verified backup.

## 7. Rollback

| Phase reached | Rollback |
|---|---|
| 1 Expand | Alembic downgrade drops the new columns/index. No data loss (plaintext untouched). |
| 2 Dual write / 3 Backfill | Set `PII_MODE=plaintext`; optionally downgrade. Plaintext is still authoritative. |
| 5 Switched reads | Set `PII_MODE=dual` (instant, plaintext still present). |
| 6 Contract | No downgrade path: run the decrypt-back script (`*_ct` → plaintext columns) with the keys, or restore from the pre-contract backup. **Losing `PII_ENC_KEYS` after contract = permanent loss of CNIC/phone**, so keys need an offline escrow copy before step 6. |

## 8. Follow-ups (not in AST-007)

1. Add a generic 500 handler (shared file `main.py`) that logs only exception class, SQLSTATE and constraint name, never `str(exc)` of DB errors, so PostgreSQL DETAIL (`Failing row contains …`) cannot reach logs.
2. Request body-size limit (roadmap AST-007 bullet; needs a middleware in `main.py`): field limits now bound every string, but a huge JSON body is still parsed before validation.
3. Rotate-and-reencrypt job and admin masked view (M5).
