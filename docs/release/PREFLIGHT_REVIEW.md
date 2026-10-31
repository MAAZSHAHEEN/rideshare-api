# Review of `handover/prod_email_preflight.sql` (AST-005 preflight)

Reviewed 31 Oct 2026 (staging-ops). Original: `handover/prod_email_preflight.sql` (30 Oct, 2,287 bytes).
Fixed copy: [`sql/prod_email_preflight.sql`](sql/prod_email_preflight.sql) (revision 2).
**Neither file was executed against production, staging or Neon.** Both were run on a disposable local
database (`rs_so_preflight`, PostgreSQL 17.11, `datctype` C.UTF-8) seeded with synthetic twins.

## 1. Verdict

The original is safe (read-only, no PII output, ends in ROLLBACK) and its **blocking check (§3) is
correct**: it groups by exactly `lower(email)`, the expression of `uq_users_email_lower` and of the
migration's own check. Its **informational checks under-report** Unicode-whitespace padding, because
PostgreSQL's `\s` is ctype-dependent and narrower than the Python `str.strip()` the API uses (AST-005
fix). Revision 2 fixes that and adds hardening; the blocking result is unchanged.

## 2. Line-by-line

| Line(s) in original | Finding | Severity | Revision 2 |
|---|---|---|---|
| `BEGIN TRANSACTION READ ONLY;` | Correct: any write raises `25006 read_only_sql_transaction` (tested). Each statement took its own snapshot under READ COMMITTED, so counts in §2 and groups in §3/§4 could disagree under concurrent sign-ups. | Low | `ISOLATION LEVEL REPEATABLE READ, READ ONLY` — one snapshot for all five queries |
| `SET LOCAL statement_timeout = '15s'` | Correct, transaction-scoped, not a config change. | — | kept |
| (missing) | No `lock_timeout`: an `AccessShareLock` read only waits behind ACCESS EXCLUSIVE (e.g. a concurrent migration or `VACUUM FULL`), but then it would queue *and block everyone behind it* until `statement_timeout`. | Low | `SET LOCAL lock_timeout = '3s'` |
| (missing) | No `idle_in_transaction_session_timeout`: in the Neon SQL Editor, a failed batch can leave the session idle in a transaction holding a snapshot (bloat, blocks `CREATE INDEX CONCURRENTLY` later). | Low | `SET LOCAL idle_in_transaction_session_timeout = '60s'` |
| §1 `datcollate, datctype` | Good: `lower()` follows the ctype. But the index uses the **column's** collation; a non-default column collation would make the database-level ctype irrelevant. | Info | adds `email_column_collation` and the definition of any existing `uq_users_email_lower` |
| §2 `email ~ '^\s\|\s$'` and `'\s'` | **Under-reports.** `\s` = `[[:space:]]` under the database ctype. glibc's C.UTF-8 does not treat U+00A0 (no-break space) as space; Python `str.strip()` does. Seeded `'carol@example.test\u00a0'` was missed (original `padded_whitespace` 4, true 5). | Medium (informational only — never blocks the migration) | explicit class equal to Python's `str.isspace()` set (ASCII 09–0D, 1C–1F, 20; U+0085, U+00A0, U+1680, U+2000–200A, U+2028, U+2029, U+202F, U+205F, U+3000) |
| §2 `not_single_at` `'^[^@\s]+@[^@\s]+$'` | Counts padded rows as malformed (double counting with `padded_whitespace`). | Info | `'^[^@]+@[^@]+$'`; adds `invisible_format_chars` (U+200B–200D, U+2060, U+FEFF: not whitespace for Python either, so such rows can never log in by typing the address) and `null_or_empty` |
| §2 `'[^\x01-\x7F]'` | Correct (ARE hex escapes). | — | written as `\u0001-\u007f` for consistency |
| §3 `GROUP BY lower(email) HAVING count(*) > 1` | **Correct** and identical to `f6b2d8e0c413.upgrade()` and to `uq_users_email_lower` (`lower(email)`, default collation). Verified: with twins present the unique index fails to build; after deleting the twins §3 reports 0 and the index builds. `json_agg` is unbounded. | — / Low | same predicate; ids capped at 200 groups while the count stays exact |
| §4 `lower(regexp_replace(email, '^\s+\|\s+$', '', 'g'))` | Same `\s` gap: missed the NBSP twin `[5,6]`. | Medium (informational) | explicit class; result now equals Python `strip()` + PostgreSQL `lower()` exactly (tested) |
| §5 padded ids, `LIMIT 100` | Good bound; same `\s` gap (missed id 5). | Medium (informational) | explicit class |
| `ROLLBACK;` | Correct. With `psql -v ON_ERROR_STOP=1`, an error exits psql and the server rolls back on disconnect. | — | kept; run instructions added in the header |
| Output | No column ever selects `email`, `name`, `phone_number` or `cnic`; only counts, user ids, and server facts. Verified: no seeded address appears anywhere in either file's output. | — | unchanged |

Trimming and Unicode parity with the AST-005 fix (`schemas.normalize_email`): the API trims with
`str.strip()` and folds **ASCII** letters only; case-insensitive identity is PostgreSQL `lower()` on
both sides of the login lookup (`routers/auth.py`) and in the unique index. So:
- blocking twins = equal under `lower(email)` (§3) — case, `İ`/`i`, `ẞ`/`ß` all fold in PostgreSQL
  under C.UTF-8 and were reported by both versions;
- "same person, different stored spelling" = equal under `lower(py_strip(email))` (§4) — only
  revision 2 matches this exactly;
- ZWSP-prefixed addresses are distinct for both Python and PostgreSQL; they are counted, not grouped.

## 3. Test on a disposable database

Script: `/workspace/work/rsso-preflight-test.py` (refuses database names not starting with `rs_so`).
Log: `/workspace/work/rsso-preflight-test.log`. 19 synthetic users: case twin, ASCII-space, NBSP,
EM SPACE, tab and ideographic-space padding, `İsa`/`isa`, `ẞtrasse`/`ßtrasse`, ZWSP, one clean.

| Output | Original | Revision 2 | Ground truth |
|---|---|---|---|
| `read_only` | on | on | — |
| `padded_whitespace` | 4 | **5** | 5 (Python `e != e.strip()`) |
| `case_duplicate_groups` (blocks) | 3: `[1,2] [9,10] [11,12]` | 3: same | 3 (PostgreSQL `lower()`); unique index fails to build |
| `trim_case_duplicate_groups` | 7 (missing `[5,6]`) | **8** | 8 (Python `strip()` → PostgreSQL `lower()`) |
| `padded_user_ids` | `[3,7,13,17]` | **`[3,5,7,13,17]`** | `[3,5,7,13,17]` |
| After deleting 2, 10, 12 | — | §3 = 0 groups | unique index builds |
| A write inside `READ ONLY` | rejected | rejected | — |
| Row count afterwards | unchanged | unchanged | — |
| Email addresses in output | none | none | — |

## 4. How Maaz runs it (approval already given for a read-only run; still his action)

1. Prefer a read-only role on the **direct** host. If only the owner role exists, the `READ ONLY`
   transaction plus `PGOPTIONS='-c default_transaction_read_only=on'` still prevents writes.
2. `psql "$URL" -X -v ON_ERROR_STOP=1 -f docs/release/sql/prod_email_preflight.sql > preflight.txt`
   (or paste the whole file into the Neon SQL Editor and run it as one batch).
3. Share only: `read_only`, `datctype`, `email_column_collation`, the §2 counts, and the §3/§4 counts.
   The id lists stay with Maaz unless a twin decision needs them.
4. `case_duplicate_groups` must be **0** before `f6b2d8e0c413`. Otherwise decide per group (merge,
   rename the stale account) — a data change needing its own approval.
