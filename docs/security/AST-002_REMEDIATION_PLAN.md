# AST-002 remediation plan: stale public `main` and historical `.env` exposure

Status: **plan only, nothing executed.** Every action in §5–§8 needs Maaz's explicit
approval (default-branch change, pushes to `main`, history rewrite, secret
rotation, GitHub settings). Investigation date: 31 Oct 2026, read-only (local
`git` on a fresh fetch, GitHub repo metadata, passive unauthenticated GETs to
production). **No secret value is reproduced in this document**; values are only
characterised (length, shape, placeholder vs. real-looking).

## 1. Summary

| Question | Finding |
|---|---|
| Is `main` stale? | Yes. `main` @ `e700ddb` (13 Jun 2026) is **0 commits ahead and 23 behind** `rideshare-v2` @ `e8da425`; `main` is an ancestor of `rideshare-v2`, so a fast-forward is possible. It is the repo's **default branch** (public, 0 forks, 0 stars at time of check). |
| Was a real production secret committed? | **No evidence of one.** No Neon host, Neon password pattern, cloud key, GitHub token or private key appears in any blob of any reachable commit (295 unique blobs scanned). The production `DATABASE_URL` and production `SECRET_KEY` were never in git. |
| What *was* committed? | (a) a public **placeholder JWT key** (26 chars; the same placeholder literal the engineering audit cites, not repeated here), hard-coded in `routers/auth.py` and in a `.env`; (b) a **local development PostgreSQL password** (9 characters, letters and digits, user `postgres`, host `localhost`), in five files. |
| Is it still visible on the default branch today? | **Yes.** `main`'s current tree still contains the placeholder key in `routers/auth.py`, a verbatim copy of the old `.env` at `routers/__pycache__/Untitled`, the local DB password in `alembic.ini`, `docker-compose.yml` and a compiled `__pycache__/database.cpython-314.pyc`. |
| Is production using the placeholder key now? | Very unlikely (inferred, not verified). Live `/openapi.json` exposes `allowed_modes` (commit `dc40207`, 11 Oct), which is later than `aaeaa44` (6 Oct), where `config.py` began refusing HS256 keys under 32 bytes; the 26-byte placeholder would stop the app at startup. |
| Was it ever used in production? | Possibly, historically. `main`'s `routers/auth.py` hard-codes the key and ignores the environment. If production ran `main` code (README "Update live API URL to FastAPI Cloud", 13 Jun 2026) until the first v2 deploy (≥ 11 Oct), tokens during that period were signed with a **publicly known key**, so anyone could have forged a token for any user. Tokens expire after 60 min and a different key is in use now, so this is closed going forward; the residual question is whether data was accessed during that window. |
| Rotation required? | `SECRET_KEY`: **recommended** (cheap, closes any doubt). DB credentials: **only if** the local password was ever reused for Neon or anything else; the production Neon password was never in git. Details §6. |
| History rewrite required? | **Not required** for security (no live production secret in history). Optional for hygiene; costs in §7. |

## 2. Method

- `git fetch origin`; `git rev-list --left-right --count origin/main...origin/rideshare-v2`; `git merge-base`.
- `git log --all --name-status -- .env '*.env' '.env.*'` to find env files ever tracked.
- A script read each historical env-like file and every unique blob in every reachable commit, and reported **only** variable names, value lengths, character classes and whether a value matched known placeholder prefixes, a known literal, or secret patterns (Neon hosts/`npg_` passwords, `rediss://user:pass@`, AWS keys, GitHub tokens, `sk-` keys, PEM private keys). Values were never printed.
- GitHub `repo_get`: visibility public, default branch `main`, forks 0. Secret-scanning status is not readable with the available token.
- Passive GETs to `https://rideshare-api.fastapicloud.dev/openapi.json` to date the deployed code.

## 3. What was committed, where, and for how long

### 3.1 Env files

| File | Added | Removed | Contents (characterised) |
|---|---|---|---|
| `.env` | `6e861fa` (26 Feb 2026, initial commit) | `d0ed9fe` (5 Mar 2026, "untrack .env") | `DATABASE_URL` → `postgresql+asyncpg`, user `postgres`, host `localhost:5432`, **9-char letters+digits password** (local dev; real-looking, not a placeholder); `SECRET_KEY` → placeholder-looking 26-char literal; `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` (non-secret). |
| `routers/__pycache__/Untitled` | `6e861fa` | `f9e607b` on `rideshare-v2` (5 Oct 2026); **still present on `main`** | byte-for-byte the same content as the `.env` above. |
| `.env.example` | `f9e607b` | (current) | placeholders only (`replace-with-…`), plus empty rate-limit secrets. Fine. |

No other `*.env`/`.env.*` file was ever tracked.

### 3.2 Other files containing those values

| Path | Value | Commits | On `main` HEAD? | On `rideshare-v2` HEAD? |
|---|---|---|---|---|
| `routers/auth.py` | placeholder JWT key, hard-coded `SECRET_KEY = …` | `6e861fa` → removed `f9e607b` | **yes** (line 14) | no (reads `config.SECRET_KEY`) |
| `alembic.ini` (`sqlalchemy.url`) | local DB password | `6173dec` (27 Feb) → removed `02ef842` (6 Oct) | **yes** | no |
| `docker-compose.yml` | local DB password | `c010a63` (4 Mar) → removed `02ef842` | **yes** | no (`${POSTGRES_PASSWORD:?…}`) |
| `__pycache__/database.cpython-314.pyc` | local DB URL incl. password (compiled constant) | `6e861fa` → removed `f9e607b` | **yes** | no |
| other `__pycache__/*.pyc` (8 files) | no secret strings found | `6e861fa` → removed `f9e607b` | yes | no |

Every remote branch descends from these commits, so the values remain in the
history of **all** branches (and in GitHub's PR refs), even where the tip is clean.

### 3.3 What else `main` exposes (non-secret, but misleading or unsafe)

- Pre-hardening code: no `SECRET_KEY` validation (hard-coded key), no departure guards, no admin-registration block, no Alembic-authoritative schema, no concurrency fixes, no CI, no rate limiter.
- Committed bytecode and the `Untitled` file.
- README that predates the FastAPI Cloud/v2 architecture (audit O6).
- Anyone who deploys `main` as-is ships forgeable tokens (audit S4).

## 4. Risk assessment

| Item | Risk now | Why |
|---|---|---|
| Placeholder JWT key on `main` | **Medium (repo/reputation), Low (live)** | Public and forge-able for anyone running `main`; live app very likely uses a different, validated key. |
| Historical use of the placeholder in production (≈ Jun–Oct 2026, if `main` was deployed) | **Unknown → treat as possible exposure** | Data present then could have been read/modified via forged tokens. No logs exist to prove either way. |
| Local DB password | **Low**, unless reused | `localhost` only; a risk only if the same password protects anything reachable (Neon role, other DBs, accounts). |
| Production Neon credentials | **None found in git** | — |

## 5. Default branch: recommendation and steps

Two options; both need approval. **Before either, confirm in the FastAPI Cloud
dashboard which branch (if any) auto-deploys.** If `main` is wired to auto-deploy,
updating `main` *is a production deploy*.

**Option A (recommended): fast-forward `main` to `rideshare-v2` after the Wave 1 PRs are merged and accepted.**
- No force-push, no history rewrite, keeps every existing link to `main`.
- Steps: (1) Wave 1 merged into `rideshare-v2`, CI green; (2) confirm no auto-deploy from `main` (or disable it); (3) `git push origin rideshare-v2:main` (fast-forward only; refuses if not FF); (4) verify `main` tree has no `__pycache__`, `Untitled`, hard-coded key or DB URL; (5) add branch protection to `main` (PR + required CI, no force-push); (6) decide whether `rideshare-v2` stays the integration branch or `main` becomes it (update `ci.yml` triggers and `DEPLOYMENT.md` accordingly).

**Option B: make `rideshare-v2` the default branch** (Settings → General → Default branch).
- Instant, no push. Open PRs keep their current bases; existing clones keep tracking `main`.
- Leaves stale `main` visible unless also fast-forwarded, archived (renamed to `legacy-main`) or deleted. Deleting `main` breaks old links; renaming makes GitHub redirect.

Either way: close or update PR #1 (`oss-readiness`) base if it targets `main`.

## 6. Secret rotation plan (approval required, in this order)

1. **Confirm facts first (read-only, Maaz):**
   - In FastAPI Cloud env settings, confirm `SECRET_KEY` is ≥ 48 random characters and is **not** the placeholder (check the length and first characters privately; do not paste it anywhere).
   - Confirm whether the local dev DB password was ever used for Neon, another database or any account.
   - Check deployment history: did production ever run `main` code (Jun–Oct 2026)?
2. **Rotate `SECRET_KEY` (recommended regardless).**
   - Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   - Set in FastAPI Cloud; redeploy/restart.
   - Impact: **all issued JWTs become invalid immediately** (no key overlap support; `ALGORITHM` stays HS256). Users are signed out and must log in again; the frontend already loses tokens on reload, so impact is low. Do it off-peak; no DB change.
   - Verify: old token → 401; fresh login → 200; `GET /me` works.
3. **Rotate Neon credentials only if** step 1 found reuse, or if any doubt remains about the production URL's exposure (e.g. it was ever pasted into a chat, issue or log).
   - Neon → Roles → reset password for the app role (or create a new role with the same grants, switch, then drop the old one).
   - Update `DATABASE_URL` in FastAPI Cloud; redeploy; verify `/readyz` (or `/test-db` before PR #7).
   - Impact: brief connection errors between the reset and the redeploy (minutes). Do role-swap to avoid the gap.
4. **Change the local dev password** wherever it is still used (local Postgres/Docker volumes) and anywhere it was reused. No production impact.
5. **Not applicable yet:** `AUTH_RATE_LIMIT_HMAC_KEY` and Redis credentials were never set in git and are not in use in production.
6. Record dates of each rotation (not values) in the ops log.

Order rationale: confirm before acting; JWT rotation first because it is cheap and also invalidates any token forged with the old placeholder; DB rotation only when justified because it carries a short outage risk.

## 7. History rewrite: assessment

**Recommendation: do not rewrite history now.** Rotation (or confirming non-reuse) is what neutralises an exposed value; the values found are a public placeholder and a localhost password, and the repo has been public since February, so the values must be assumed copied already.

If Maaz still wants them gone from history (hygiene):

- Tool: `git filter-repo` (not `filter-branch`) with `--path .env --path routers/__pycache__ --path __pycache__ --invert-paths` and `--replace-text` for the two literals (supplied from a local file, never committed).
- Costs and risks:
  - Every commit SHA changes on every branch: all 9 open PRs (#2–#10) and their review history point at old SHAs and must be recreated or force-pushed; the ledger, merge-readiness report and handover docs that cite SHAs become stale.
  - Requires force-pushing all branches; collaborators/agents must re-clone (worktrees under `/workspace/work/rs-*` included).
  - Forks: 0 today. Any future fork or existing clone keeps the old history; GitHub may keep cached views and PR refs until GitHub Support purges them.
  - Easy to lose work if a branch is missed.
- If done, do it **after** Wave 1 PRs merge (fewer open branches), in one window, and ask GitHub Support to remove cached refs.

## 8. GitHub-side controls (approval required for settings)

- Settings → Code security: enable **Secret scanning** and **Push protection** (free for public repos); review any existing alerts.
- Add a `gitleaks` (or `trufflehog`) job to CI on pull requests (audit §6.4 recommends gitleaks). Mark it required after one green run.
- Keep `.gitignore` entries for `.env`, `__pycache__/`, `*.pyc` (already on `rideshare-v2`).
- Branch protection on `main` and `rideshare-v2`: PRs only, required checks, no force-push.
- Optional: `CODEOWNERS` and Dependabot security updates (Dependabot config is in PR #10).

## 9. Approval package

| # | Action | Why | Risk | Rollback | Cost of postponing |
|---|---|---|---|---|---|
| 1 | Confirm prod `SECRET_KEY` is not the placeholder; confirm no DB password reuse; check whether `main` was ever deployed | Grounds every other decision | none (read-only, by Maaz) | — | decisions stay guesses |
| 2 | Rotate `SECRET_KEY` | Removes any doubt; kills forged tokens | everyone signed out once | set the old key back (only if the new one breaks startup) | low daily risk, but an open question in any security review |
| 3 | Fast-forward `main` to `rideshare-v2` after Wave 1 (Option A), after checking auto-deploy | Public default shows current, safe code; removes the `.env` copy and hard-coded key from the visible tree | if `main` auto-deploys, it deploys | push previous SHA back (needs force-push) or redeploy previous build | reviewers and anyone cloning see insecure code |
| 4 | Enable secret scanning + push protection; add gitleaks CI | Prevent recurrence | none | disable | — |
| 5 | Rotate Neon credentials (conditional) | Only if reuse/doubt | minutes of DB errors without role swap | revert `DATABASE_URL` to old role before dropping it | — |
| 6 | History rewrite (not recommended now) | Hygiene only | high churn, breaks PRs/SHAs | none practical | none for security |
