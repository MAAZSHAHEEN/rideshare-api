# Historical secrets in git and the rotation checklist

Prepared 31 Oct 2026 (staging-ops). Extends `docs/security/AST-002_REMEDIATION_PLAN.md` (PR #11) with
a fresh scan of **every ref, including all PR heads and PR merge refs**, and a step-by-step rotation
checklist. **No value is reproduced here; nothing was rotated; no production system was accessed.**
Rotation, default-branch change and GitHub settings are approval-gated (LB-2).

## 1. Scan

- Mirror clone of `MAAZSHAHEEN/rideshare-api` (`git clone --mirror`: all `refs/heads/*` and
  `refs/pull/*/head` + `refs/pull/*/merge` for PRs #1–#28), 139 commits.
- gitleaks 8.30.1, `git --log-opts=--all --redact=100`, twice: default rules, and default + two
  custom rules (`db-url-with-password`, `secret-key-assignment`, the same `gitleaks-extra.toml` the
  RC integration used).
- Classification by a script that read each finding locally and printed only rule, file, commit,
  value length, character class, a short sha256 fingerprint, placeholder markers and the URL host.
- Default rules: 7 findings. Default + custom: 33 findings, 28 distinct (rule, file, value).

## 2. Findings (redacted)

| # | Commit(s) | File | Kind | Characterisation | Real secret? | Ever valid in production? |
|---|---|---|---|---|---|---|
| F1 | `6e861fa` (26 Feb 2026, initial) — removed `d0ed9fe` | `.env` | DB URL password | 9 chars, letters+digits, user `postgres`, host `localhost` | **real-looking local dev password** | **Unknown.** Host is localhost; the production `DATABASE_URL` (Neon) was never in git (AST-002 §1). Valid in prod only if the same password was reused for a Neon role — Maaz must confirm. |
| F2 | `6e861fa` → removed `f9e607b` on v2; **still on `main`** | `routers/__pycache__/Untitled` | same DB URL + same JWT key as F1/F3 (byte-identical copy of `.env`) | same fingerprints as F1 and F3 | as F1/F3 | as F1/F3 |
| F3 | `6e861fa` → removed `f9e607b` on v2; **still on `main`** | `.env`, `routers/auth.py`, `Untitled` | `SECRET_KEY` literal | 26 chars, letters+digits, same value in all three files | **public placeholder-style key** | **Possibly, historically**: `main`'s `routers/auth.py` hard-codes it; if production ran `main` code (Jun–Oct 2026) tokens were signed with a publicly known key. Not valid now (inferred): `config.py` since `aaeaa44` rejects HS256 keys < 32 bytes and the live API exposes post-`dc40207` fields. **Unknown** for the period before — no deploy history available to Hark. |
| F4 | `f9e607b`, `02ef842` | `.env.example` | DB URL password, SECRET_KEY | contain `your`/`replace`/`password`/`secret` markers | placeholder | no |
| F5 | `9c663f9` | `README.md` | DB URL password, SECRET_KEY | `your…` placeholders | placeholder | no |
| F6 | `2ff266e` | `CONTRIBUTING.md` | DB URL password, SECRET_KEY | password is the literal word "password" (fingerprint matches sha256("password")); key contains "test" | placeholder | no |
| F7 | `0e00d58`, `401661d` | `.github/workflows/ci.yml` | DB URL password, SECRET_KEY | `ci-only-disposable-…-not-for-production` | CI-only, documented as public | no |
| F8 | `364d8ad` | `tests/test_health.py` | DB URL with password | test fixture, host `127.0.0.1` / none | test value | no |
| F9 | `8eb22e6` | `tests/test_safe_errors.py`, `tests/safe_errors_server_app.py` | DB URL with password | contains "fake", host `db.internal` | test value (asserts it never reaches logs) | no |
| F10 | `48f7580` (#27) | `tests/tools/m3_db_checks.py` | DB URL password ×2, generic-api-key | disposable local role password ("rs-stress-disposable-…") | test value | no |
| F11 | `4873712` (#10) | `tests/test_jwt_compat.py` | 6 JWTs | claims `sub, role, exp(=2147483647), [iat]`; signed with a test-only golden key in the same file | test tokens | no (production key differs, see F3) |
| F12 | `61b915b` (#11) | `docs/security/AST-002_REMEDIATION_PLAN.md` | DB URL with password | 4-char example in a documentation snippet | placeholder | no |

No Neon host, Neon password pattern, cloud API key, GitHub token, private key or Redis credential
appears in any blob on any ref. The only items that could ever have been valid outside a laptop are
**F1/F2 (only if reused)** and **F3 (if `main` was deployed)**. Every branch and PR ref descends from
`6e861fa`, so F1–F3 stay in history everywhere even where the tip is clean; `main`'s **current tree**
still contains F2/F3.

Recommended addition to CI (approval-gated settings aside): a gitleaks job on pull requests with an
allowlist for F4–F12 paths, and GitHub secret scanning + push protection (AST-002 §8).

## 3. Rotation checklist (approval required for every step; Maaz executes)

### 3.0 Confirm first (read-only, Maaz, 10 min)

- [ ] FastAPI Cloud env: production `SECRET_KEY` length ≥ 48 and **not** the F3 value (compare
      privately by length/first characters; never paste).
- [ ] Was the F1 password ever used for a Neon role, another database, or any account? (yes → §3.2)
- [ ] FastAPI Cloud deploy history: did production ever run `main` code? (yes → F3 was live; treat
      data from that period as possibly accessed; §3.1 is mandatory)
- [ ] Neon roles list: which role does the app use; is it the owner? (feeds SECURITY_REVIEW_RC §5)

### 3.1 `SECRET_KEY` (JWT signing) — recommended regardless

Impact: **every issued token becomes invalid at once** (single key, no key ring). Users are signed
out and log in again. `token_version` (`tv`, #19) is per-user and does not help or hurt here: a key
change invalidates all tokens including those with a current `tv`; `token_version` values stay as
they are and need no change. Admin tokens are invalidated too.

- [ ] Coordinate with the RC deploy (one sign-out instead of two) or do it off-peak.
- [ ] Generate locally **two** fresh keys: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
      twice → **K-new** (goes into FastAPI Cloud env now) and **K-standby** (stored only in the password
      manager, labelled "standby, never deployed"). Paste only into FastAPI Cloud env. The old value is
      **not** kept as a rollback option (§3.5): if it is F3 or might be, it is burned; if it is a never-
      leaked key, it still is not used again (rolling back to it would re-validate every token it signed).
- [ ] Redeploy / restart so the new value is loaded (config is read at import).
- [ ] Verify: an old token → `GET /me` 401; fresh login → 200 and `GET /me` 200; decoded header
      `alg=HS256`; `/readyz` ready.
- [ ] Staging/preview keys are **different** from production (NEON_STAGING_PLAN §9).
- [ ] Rollback (only if K-new breaks startup or verification): deploy **K-standby**, never the old
      value. Effect is the same as the rotation itself (everyone signs in again). Then generate a new
      standby. See §3.5 for why and for the dual-key window proposal.
- [ ] Record date/time (not the value) in the ops log.

### 3.2 Database password (only if §3.0 found reuse or any doubt)

Zero-gap role swap (preferred over resetting the password in place):

- [ ] Neon: create `rideshare_app_v2` with the same grants as the current app role (least-privilege
      grants from SECURITY_REVIEW_RC §5 if the role split is being done anyway).
- [ ] Update `DATABASE_URL` in FastAPI Cloud (pooled host for the app); redeploy.
- [ ] Update `JOBS_DATABASE_URL` (direct host) if the jobs role is affected.
- [ ] Verify: `/readyz` ready; login + one booking flow on a staff account; Neon
      `SELECT usename, count(*) FROM pg_stat_activity GROUP BY 1` shows the new role only.
- [ ] Immediately (not after 24 h) when the old password is or might be F1: `ALTER ROLE <old> NOLOGIN`
      and terminate its sessions once `/readyz` is green on the new role; drop it after 24 h.
      If the old password is not suspected: `NOLOGIN` after 24 h with no errors, then drop.
- [ ] Rollback: **never** back to the old role/password. Before the swap also create
      `rideshare_app_v2b` (same grants, its own freshly generated password, stored in the password
      manager as "standby, never deployed"). If v2 misbehaves, point `DATABASE_URL` at v2b. See §3.5.
- [ ] Local: change the F1 password on every machine/Docker volume that still uses it.

### 3.3 Other secrets

| Secret | In git? | In use in prod? | Action |
|---|---|---|---|
| `AUTH_RATE_LIMIT_HMAC_KEY`, Redis URL | no | no (rate limiting off) | create fresh when LB-3 is approved; never reuse staging values |
| `JOBS_DATABASE_URL`, `JOBS_HEARTBEAT_URL` | no | not yet | create fresh (SCHEDULER_AND_OPS §6) |
| Future PII keys (`PII_KEK_*`, `PII_BIDX_KEY_*`) | no | not yet | PRIVACY_CNIC_PHONE §4; escrow before contract |
| GitHub tokens / deploy tokens | none found | unknown | review FastAPI Cloud deploy tokens list; revoke unused |

### 3.4 After rotation

- [ ] Default branch / fast-forward `main` (AST-002 §5) so the public tree no longer shows F2/F3.
- [ ] Enable secret scanning + push protection; add the gitleaks PR job.
- [ ] History rewrite: **not recommended** (AST-002 §7) — rotation is what neutralises a value.

### 3.5 Compromised values are never rollback credentials (added 31 Oct 2026, a1-docs)

**Rule.** A secret that is, or may be, exposed — **F1** (local DB password in git history; reuse
unknown), **F3** (26-char JWT key hard-coded on `main`; possibly valid historically), anything a future
scan finds, and any value that was ever pasted into chat, a screenshot, a ticket or a log — is
**burned**: it is never redeployed, never used as a "known good" fallback, never reused for staging,
tests or another service. "Rollback" of a rotation always means moving **forward to another freshly
generated value**, never back.

| Secret | Rotation | Rollback means | Never |
|---|---|---|---|
| `SECRET_KEY` (JWT) | K-new deployed | deploy K-standby (fresh, never deployed before) | the previous key, F3, any staging key |
| App DB role/password | role swap to `rideshare_app_v2` | switch to `rideshare_app_v2b` (pre-created, own fresh password) | the old role's password, F1, a reset back to a remembered value |
| Jobs DB URL / heartbeat / rate-limit HMAC | fresh per environment | another fresh value | copying prod ↔ staging values |

**Dual-key JWT window (proposal for the auth owner, #10/#13 — not implemented on #25).** Today the app
has one `SECRET_KEY` (`config.py`; `jwt.encode`/`jwt.decode` in `routers/auth.py` / `routers/dependencies.py`),
so any key change signs everyone out at once and a "rollback" can only be a full second sign-out. A
small key ring would allow zero-sign-out rotation **without** ever trusting an old compromised key:

- env `JWT_SIGNING_KEYS` = JSON `{"<kid>": "<key>", …}`, `JWT_ACTIVE_KID` = the kid used to sign;
  tokens carry `kid` in the JWS header; verify looks up the key by `kid` from the ring only (unknown
  `kid` → 401; `alg` still pinned).
- Planned rotation: ring = {new primary (active), new standby}; the *previous* key may stay in the ring
  for at most `ACCESS_TOKEN_EXPIRE_MINUTES` (60 min) **only if it is not suspected**; a suspected key
  (F3, or any doubt) is removed immediately — users signed with it re-login, which is the point.
- Rollback = flip `JWT_ACTIVE_KID` to the standby kid; the old key never returns.
- `token_version` (`tv`) is unaffected (per-user), as today.

**Denylist of known-compromised values at startup (proposal, follow-up for the #10/#13 owner).**
`config.py` already rejects F3 indirectly (HS256 keys < 32 bytes and placeholder prefixes fail at
import), but a future leaked value of valid length would pass. Proposed addition, ~10 lines, no
plaintext stored:

```python
# config.py (proposal) — SHA-256 hex digests of burned SECRET_KEY / DB password values.
# Add a digest when a value is burned; never add the value itself.
_COMPROMISED_SHA256 = frozenset(filter(None, os.getenv("COMPROMISED_SECRET_SHA256", "").split(","))) | {
    # "<sha256 of F3>",  # filled by Maaz locally: python -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
}
def _burned(value: str) -> bool:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() in _COMPROMISED_SHA256
if _burned(SECRET_KEY):
    raise RuntimeError("SECRET_KEY is on the compromised-secret denylist; generate a new key")
_db_pw = urllib.parse.urlsplit(DATABASE_URL).password  # where DATABASE_URL is parsed
if _db_pw and _burned(_db_pw):
    raise RuntimeError("DATABASE_URL uses a compromised password; rotate the role")
```

Notes: an unsalted SHA-256 of a 26-char random key is not brute-forceable, but the digest of a short
password (F1 is 9 chars) **is** guessable offline — so the F1 digest goes only into the deploy env
(`COMPROMISED_SECRET_SHA256`), never into the repository; the F3 digest may be committed. Tests: a unit
test with a test-only burned value (never a real one) asserting the import fails. Owner: auth/config PR
owner (#10 PyJWT / #13 config); **not** edited on #25.

**Revocation verification (after every rotation or rollback):**

- [ ] JWT: a token minted before the change → `GET /me` **401**; a token signed locally with the old
      key (Maaz, locally, never in chat) → 401; a token with `alg=none` → 401; fresh login → 200.
- [ ] JWT (once the key ring exists): token with the removed `kid` → 401; unknown `kid` → 401.
- [ ] DB: `psql` with the old role/password → `FATAL: password authentication failed` or
      `role … is not permitted to log in`; Neon `SELECT usename, count(*) FROM pg_stat_activity GROUP BY 1`
      shows no session of the old role; `SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = '<old>'`
      → `false` (or no row after the drop).
- [ ] Startup denylist (once implemented): deploying with a burned value fails fast at import (check on a
      preview, never on production).
- [ ] Ops log: date/time, which secret, which standby was consumed (labels only, never values); create
      the next standby the same day.
