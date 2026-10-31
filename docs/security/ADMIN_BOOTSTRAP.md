# Admin bootstrap and admin role changes

Status: **implemented on `feat/admin-bootstrap` (draft PR), never run against production.**
Running it against production, or creating any real admin account, needs **Maaz's written
approval** (decision D1 in `docs/architecture/TRUST_AND_SAFETY.md`; launch blocker LB-9 /
finding S4 in `docs/release/SECURITY_REVIEW_RC.md`).

## 1. What exists

- **No HTTP endpoint changes a role.** The admin API can view, suspend and reinstate accounts only.
  `tests/test_admin_bootstrap.py::ApiEscalationTests` pins this: the `/admin` route list is
  fixed, no request body schema other than `UserCreate` has a `role` field, `UserCreate.role` is
  limited to `passenger | driver`, and privileged extra fields (`is_admin`, `account_status`,
  `token_version`, `id`, …) are ignored on registration and admin bodies.
- **One operator CLI**: `python -m scripts.grant_admin`. It is not part of the API image (the
  Dockerfile copies `*.py`, `routers/`, `services/`, `alembic/` only), so it cannot be reached
  from the deployed app. It runs from an operator's checkout, with a database URL.

## 2. Usage

```bash
# In the operator's own shell. Generate a one-off token, never commit or paste it anywhere.
export ADMIN_BOOTSTRAP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DATABASE_URL=...            # see §5 for which role

# First admin. Refused if ANY admin account exists (suspended admins count).
python -m scripts.grant_admin --bootstrap --email ops-admin@example.com \
    --reason "Initial admin approved by Maaz" --confirm
# prompts: Admin bootstrap token  (type the value of $ADMIN_BOOTSTRAP_TOKEN)

# Later grants and every revoke: an existing active admin proves who they are.
python -m scripts.grant_admin --email second-admin@example.com \
    --actor-email ops-admin@example.com --reason "Second on-call admin" --confirm
python -m scripts.grant_admin --revoke --to-role passenger --email second-admin@example.com \
    --actor-email ops-admin@example.com --reason "Rotation ended" --confirm
# prompts: token, then the password of --actor-email

unset ADMIN_BOOTSTRAP_TOKEN
```

`--secrets-stdin` reads the token (and the actor password) as lines from stdin instead of
prompts, for scripted rehearsals. Exit codes: `0` done, `1` refused (state), `2` usage,
`3` not authorized, `4` production-looking database.

## 3. Authorization design (and why)

| Check | Applies to | Why |
|---|---|---|
| `--confirm` | all | Nothing is read or written without it; stops copy-paste accidents. |
| `ADMIN_BOOTSTRAP_TOKEN` in env (≥ 32 chars, not a placeholder) **and** the same value typed at the prompt, compared with `hmac.compare_digest` | all | Two independent things the operator must hold. Not a CLI flag, so it never lands in shell history or `ps`. Lives only in the operator shell for the duration of the run, never in FastAPI Cloud or CI. |
| Zero admin accounts (any status) | `--bootstrap` | Bootstrap is a one-time path. Once any admin exists, a new admin needs an existing admin. A suspended admin still blocks bootstrap, so suspension cannot be used to reopen it. |
| `--actor-email` = existing **active** admin + that admin's **password** (bcrypt-verified) | grant/revoke after bootstrap | Real identity, not a typed name: the audit row's `actor_id` is someone who proved it. |
| Actor ≠ target | grant/revoke | No self-escalation or self-demotion. Consequence: a revoke always leaves at least the actor as admin, so the last admin cannot be removed by this path. |
| Target exists, exactly one case-insensitive email match | all | Ambiguous legacy case twins are refused, never guessed. |
| Target active | grant | Never hand admin to a suspended account. |
| Target may be suspended | revoke | Compromise response: suspend first (API), then revoke; the account stays suspended. |
| Non-local `DATABASE_URL`, a database name containing `prod`, or `APP_ENV/ENVIRONMENT/ENV/RIDESHARE_ENV=production` | all | Fails closed (only `localhost`, `127.0.0.1`, `::1`, `db`, `postgres`, `*.localhost` and unix sockets count as local). Needs `--i-understand-production`, which prints a warning that Maaz's written approval is required. Checked before any secret is read or any connection is opened. |

**Honest limit.** These checks stop accidents and make every role change attributable. They do
not stop someone who already holds a database credential that can `UPDATE users SET role`: that
is what the role split in `DB_LEAST_PRIVILEGE.md` fixes (the API role loses `UPDATE (role)`,
and a guard trigger rejects any new `admin` row unless the session is the operator role).

## 4. Effects (one transaction)

1. `pg_advisory_xact_lock(0x525341444D494E00)` serializes all runs, so two concurrent bootstraps
   produce exactly one admin (tested). The job locks use the two-int4 key space, which never
   overlaps.
2. Target row `FOR NO KEY UPDATE`, actor row `FOR SHARE`, in id order (User-first, like the API);
   states re-checked after the locks.
3. `role` changes, `token_version + 1`. Every token issued before the change fails (`tv` and
   role mismatch → 401); the user must log in again (tested over HTTP).
4. One `admin_audit_log` row via `audit.record()` (flush, never commit), action
   `admin_granted` / `admin_revoked`:
   - bootstrap: `actor_id = NULL`, `metadata.actor = "system:bootstrap"`;
   - later: `actor_id = <acting admin id>`, `metadata.actor = "admin_cli"`;
   - `metadata`: `mode`, `previous_role`, `new_role`, `token_version`, `target_status`. No email,
     name, CNIC or phone; the reason is checked like API reasons (no ≥ 7-digit runs).
5. Any failure (including the audit insert) rolls everything back (tested).

## 5. Which database role runs it

- Today (single owner role): any operator with the owner URL. Unaudited raw SQL is equally
  possible, which is why the role split matters.
- After the split (`DB_LEAST_PRIVILEGE.md`): run with the **`rideshare_admin_ops`** URL. That role
  can read `users`, update only `role`, `token_version`, `updated_at`, and append audit rows. The
  API role cannot run this CLI successfully (permission denied, rolled back; tested in
  `tests/test_db_least_privilege.py`).

## 6. Production runbook (requires Maaz's written approval first)

1. Maaz decides D1: who the admins are; recommended a dedicated admin-only account per person,
   not a personal passenger/driver account (an admin has no rider/driver modes).
2. D8 hardening is still open (shorter admin token lifetime, TOTP). Until then, keep zero admins
   in production, or accept the risk in writing.
3. Create the account through normal registration, then run `--bootstrap` from a trusted machine
   with `--i-understand-production`, the token in the shell only.
4. Verify: `GET /admin/audit-log` as the new admin shows the `admin_granted` row.
5. `unset ADMIN_BOOTSTRAP_TOKEN`; the token is single-use in practice (bootstrap refuses forever
   after the first admin).

## 7. Decisions recorded

- **D-TF-1 (admin-on-admin suspension): kept allowed, audited.** An admin can suspend another
  admin (needed when an admin account is compromised); self-action stays 403. The suspend and
  reinstate audit rows now carry `metadata.target_role`, so admin-on-admin actions are visible
  in `GET /admin/audit-log`. A suspended admin's token dies immediately (it cannot retaliate),
  and reinstatement needs a fresh login. Open option for D8: require two admins or the operator
  CLI to suspend an admin.
- **D-TF-2: `admin_granted` / `admin_revoked` action names** as specified by the RC directive
  (the existing actions use `user.*`; both styles pass `audit.ACTIONS`).
