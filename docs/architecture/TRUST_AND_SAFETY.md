# Trust and safety: admin, suspension, driver eligibility, reports, audit, privacy (Milestone 5)

Status: **design + code-only foundation.** Base `rideshare-v2` @ `e8da425`.
Implemented on `feat/trust-safety-foundation` (no migration, no route mounted,
no response shape changed):

- `routers/dependencies.require_admin` and `routers/admin.admin_router()`, the single admin gate;
- `privacy.mask_cnic` / `privacy.mask_phone`, fail-closed display masks;
- `verification.DriverVerificationStatus` + `driver_verification_status()`, which can only return `not_verified`;
- tests: `tests/test_admin_guard.py`, `tests/test_trust_privacy.py`.

Everything else here is a **proposal**. Every schema change in section 11 is
designed only, and must be written as Alembic revisions **after the Wave 1 head
`f6b2d8e0c413`**, never on a branch cut from `rideshare-v2`. Production and
staging migrations, env/secret changes and any vendor sign-up stay behind
Maaz's approval gates.

> Safety rule for every PR that follows this document: **RideShare does not verify
> drivers today.** A database boolean, a self-declared field or an uploaded photo
> nobody checked is not verification. No API field, UI label, email or marketing
> copy may say or imply "verified", "trusted", "safe" or "background checked"
> until the matching evidence has been checked by a named process (section 6) and
> recorded in the audit log (section 9).

Companion documents: `docs/privacy/CNIC_PHONE_PROTECTION.md` (AST-007, branch
`fix/input-validation`, encryption design) and
`docs/architecture/BOOKING_LIFECYCLE.md` (M4, branch
`feat/booking-lifecycle-guards`, lock order and state tables). Where this
document touches rides and bookings it reuses the M4 lock order
**Ride → Booking(s), bookings by id ASC**.

---

## 1. Current state (as is, `e8da425`)

### 1.1 Role model

| Item | Fact | Source |
|---|---|---|
| Roles | `UserRole` = `passenger`, `driver`, `admin` (PostgreSQL enum `userrole`) | `models.py` |
| Self-registration | Only `passenger` or `driver` (`Literal[...]`); `admin` → 422 | `schemas.UserCreate`; test `test_admin_role_cannot_be_self_registered` |
| Capabilities | `allowed_modes(role)`: passenger **and** driver accounts both get `["passenger","driver"]`; admin gets `[]` | `permissions.py` |
| Driver eligibility | **None.** Any registered account can publish rides and accept passengers | `services/rides.create_ride`, `services/bookings.respond_to_booking` |
| JWT | HS256, claims `sub`, `role`, `iat`, `exp`; lifetime `ACCESS_TOKEN_EXPIRE_MINUTES` (default 60) | `routers/auth.create_access_token`, `config.py` |
| Token validation | Algorithm pinned, claims type-checked, user re-read from DB, **DB role must equal the signed role** (otherwise 401) | `routers/dependencies.get_current_user` |
| Revocation | **None.** No `status`, no `token_version`; a token is valid until `exp` unless the user row is deleted or its role changed | same |
| Admin creation | Only by a direct database write (no API, no script) | — |

### 1.2 What an admin can do today

Authenticate, `GET /me`, `GET /rides/` (search), `GET /rides/{id}`. Every write
and every private list returns 403 for admin (`Only drivers…`, `Only passengers…`),
covered by existing tests (`test_rides.py:212`, `test_booking_responses.py`
`test_non_owner_and_admin_cannot_respond`, `test_cancel_requires_owner_passenger`).
**There is no admin endpoint, no moderation tool, no audit trail.** The frontend
shows admins a notice that no admin console exists (`pages/Dashboard.tsx`).

### 1.3 Other trust-relevant facts

- No vehicle data at all (no make, plate or seats beyond `rides.available_seats`).
- No reports, blocks, ratings or emergency features.
- `/test-db` is unauthenticated (AST-010 branch replaces it with `/healthz` + `/readyz`).
- On `rideshare-v2` the account page still says **"Your account details, verified by RideShare."**; that false claim is removed on `fix/frontend-copy-contrast` (AST-004), not yet merged.
- CNIC and phone are stored in plaintext (`users.cnic` UNIQUE, `users.phone_number`), and **no response returns them**: `UserResponse` = id, name, email, role, allowed_modes; `PassengerSummary` (driver's booking list) = id, name; ride and booking responses carry only ids. A new test (`ResponsePrivacyGuardTests`) now fails if any response model gains a `cnic`/`phone`/`password` property.
- Any authenticated user can read any ride by id, including `driver_id` (an integer, no PII).

## 2. Threats

| # | Threat | Today | Mitigation in this design |
|---|---|---|---|
| T1 | Anyone registers and drives strangers between cities | Open | Eligibility gate on `create_ride`/`respond_to_booking` once `driver_profiles.status = approved` (§6), behind a flag |
| T2 | A reported abusive user keeps acting for up to 60 min (or forever, as no suspension exists) | Open | `users.status` + `token_version`, checked on every request (§5) |
| T3 | Users mistake "a driver account" for "a checked driver" | Partly (false copy on v2) | Honest labels (§10); `verification.py` can't return anything but `not_verified`; guard test against `verif*` fields |
| T4 | Privilege escalation to admin | Mitigated: no self-registration; DB role must match the signed role | Single `require_admin` gate; router-level dependency; inventory test fails on any unguarded `/admin` route (§4) |
| T5 | A compromised or careless admin abuses power (mass suspension, PII browsing) | N/A yet | Append-only audit log with reason codes (§9); PII masked by default, full reveal is a separate audited action (§10); no admin acts on own account |
| T6 | CNIC/phone leak through responses, logs or dumps | Responses clean; plaintext at rest | Masks for every non-owner view (implemented helpers); AST-030 encryption (CNIC doc §4) |
| T7 | Harassment via reports (false or mass reports) | N/A | Reporter must share an accepted booking with the subject; per-user report rate limit; reports never auto-suspend |
| T8 | Evidence documents leak (CNIC scans, licences) | N/A | Private bucket, signed short-lived URLs only to admins, retention deletion after decision (§7) |
| T9 | Stale approvals (licence expires, car sold) | N/A | Expiry dates stored; daily job moves expired approvals to `suspended` with reason `document_expired` |
| T10 | Ban evasion (new account, same person) | N/A | CNIC uniqueness already enforced (digits-normalised by AST-007; blind index later); suspension keyed on the user, and the CNIC blind index stays reserved after deactivation |

## 3. Roles and capabilities (target)

Keep the stored `role` column for compatibility. Add **state**, not more roles:

| Account | Can passenger | Can drive | Admin API |
|---|---|---|---|
| `passenger`/`driver` role, `status=active`, no approved driver profile | yes | **only while `DRIVER_ELIGIBILITY_ENFORCED=false`** (today's behaviour); no once enforced | no |
| same, `driver_profiles.status=approved` **and** an approved vehicle | yes | yes | no |
| any, `status=suspended` | no (403) | no (403) | no |
| `admin`, `status=active` | no | no | yes |

`permissions.allowed_modes()` becomes `allowed_modes(user, driver_profile)`;
the docstring already reserves that place. `UserResponse.allowed_modes` then
reflects eligibility, which the UI already reads; that is an additive behaviour
change, not a shape change.

## 4. Admin-only API surface

**Implemented now:** `require_admin` (401 for any authentication failure,
403 `Admin access required` for an authenticated non-admin) and
`admin_router(prefix)`, which puts `require_admin` in the router's own
`dependencies` so no endpoint can forget it. The router is **not mounted** in
`main.py` (shared file; no endpoint exists yet). `unguarded_admin_routes(app)`
in `tests/test_admin_guard.py` walks every route's dependency tree and fails if
anything under `/admin` lacks the guard.

Rules for every admin endpoint:

1. Registered on an `admin_router`; mounted with one `app.include_router` line in the same PR as the first endpoint.
2. Every mutation takes `reason_code` (enum) and optional `note` (≤ 500 chars, no PII), and writes `admin_audit_log` **in the same transaction**.
3. An admin may not act on their own account (403).
4. Read endpoints return masked CNIC/phone (`privacy.mask_*`). Full values only through `POST /admin/users/{id}/reveal-pii` with a reason, audited, rate-limited (e.g. 20/day per admin).
5. Pagination limits match the public API (`limit ≤ 100`).
6. Admin accounts are created only by an operator script (`python -m scripts.grant_admin --user-id N`, which writes an audit row with `actor = NULL`, `action = admin_granted`). Running it on production is an approval-gated production write.

Proposed endpoints (each its own PR, in this order):

| Endpoint | Purpose | Needs |
|---|---|---|
| `GET /admin/users?query=` | find by id or email (exact, case-insensitive) | audit log for reads of PII-bearing views (optional) |
| `POST /admin/users/{id}/suspend`, `/reinstate` | §5 | M5-A, M5-B |
| `GET /admin/driver-applications?status=` , `POST …/{id}/claim`, `/approve`, `/reject` | §6 | M5-C |
| `GET /admin/vehicles?status=`, `POST …/{id}/approve`, `/reject` | §7 | M5-C |
| `GET /admin/reports?status=`, `POST /admin/reports/{id}/triage`, `/resolve` | §8 | M5-D |
| `GET /admin/audit-log?target_type=&target_id=` | §9 | M5-B |

Admin tokens: same JWT, but consider a shorter lifetime (15 min) and later
mandatory TOTP for admin login. **Decision for Maaz** (D8).

## 5. Account suspension

### 5.1 Data (migration M5-A)

- `users.status VARCHAR(16) NOT NULL DEFAULT 'active'` with `CHECK (status IN ('active','suspended','deactivated'))`. The server default fills legacy rows, so the CHECK cannot fail on existing data.
- `users.token_version INTEGER NOT NULL DEFAULT 0`.
- `users.status_changed_at TIMESTAMPTZ NULL`.
- Reasons and actors live in `admin_audit_log`, not on the user row.

VARCHAR + CHECK rather than a PostgreSQL enum: adding a value later is a
constraint swap inside a transaction, not `ALTER TYPE … ADD VALUE`.

### 5.2 Effects

| Surface | Behaviour |
|---|---|
| Login | Password verified first (same timing as today). Suspended + correct password → **403 `Account suspended`**. Wrong password → the usual 401. Enumeration needs the password, so revealing suspension to the owner is acceptable. **Decision for Maaz** (D6): show "suspended", or a generic 401. |
| Tokens already issued | JWT gains `tv` (= `token_version` at issue). `get_current_user`: `tv` ≠ DB → 401 `Invalid token`; DB `status ≠ active` → 403 `Account suspended`. Suspension **and** reinstatement both bump `token_version`, so no pre-suspension token ever works again. Effect is immediate on the next request (the user row is already re-read per request). |
| Transition | Tokens without `tv` are accepted only while `TOKEN_VERSION_ENFORCED=false`. Turn it on after one full token lifetime (60 min) past deploy. Setting the flag is an env change (approval gate). |
| Driver's future active rides | In the suspension transaction: lock the driver's `active` rides with `departure_time > now` (id ASC), then their bookings (id ASC), per the M4 lock order. Ride → `cancelled`; pending/accepted bookings → `cancelled` through `lifecycle.apply_booking_transition` (M4). One audit row per ride. Passengers see the ride as cancelled (existing UI state). Notification is the email outbox (AST-033), later. |
| Driver's departed but uncompleted rides | Left untouched, listed in the admin response for manual follow-up (possible incident in progress). |
| Passenger's bookings | Pending/accepted bookings on future rides → `cancelled`, seats restored (accepted only), same transaction, same lock order. |
| Search | `search_rides` also filters `driver.status = 'active'` (join), a second line of defence if a cancellation is missed. |
| Reinstatement | `status = active`, `token_version += 1`. Cancelled rides and bookings are **not** restored. |
| Deactivation (self-delete later) | Same as suspension plus PII retention rules (§10.4). |

Policy alternative (D5): freeze rides instead of cancelling. Not recommended:
passengers would keep waiting on a driver who can't act.

### 5.3 Tests the PR must carry

Suspend → next request 403; old token after reinstatement → 401; missing `tv`
with enforcement on/off; suspension cancels exactly the future rides/bookings
and restores the right seats; race: suspension vs booking accept (lock order,
`asyncio.gather` + forced order as in M4); admin cannot suspend self; audit row
in the same transaction (rollback leaves no row); search hides a suspended
driver's ride.

## 6. Driver eligibility

### 6.1 State machine (`driver_profiles.status`)

```
not_verified ──(driver submits)──▶ submitted ──(admin claims)──▶ under_review
     ▲                                  ▲                          │      │
     │                                  └──(driver resubmits)── rejected  │
     │                                                                    ▼
     │                                         suspended ◀──(admin/system)── approved
     │                                             │
     └──── (no transition back to not_verified)    └──(admin reopens)──▶ under_review
```

| From → To | Who | Evidence / guard | Side effects |
|---|---|---|---|
| (none) → `not_verified` | system | implicit: no profile row = `not_verified` | — |
| `not_verified` → `submitted` | the driver | licence number + expiry, CNIC front/back images, licence images, at least one vehicle (§7) with registration image, consent to processing | documents stored privately; audit `driver_submitted` |
| `rejected` → `submitted` | the driver | same, after a cooldown (e.g. 24 h) | audit |
| `submitted` → `under_review` | admin | claim (assigns `reviewer_id`); one reviewer at a time | audit |
| `under_review` → `approved` | admin ≠ the driver | **completed checklist** (§6.2) stored as JSON on the decision; licence expiry in the future | `allowed_modes` includes `driver` (when enforcement on); audit with checklist |
| `under_review` → `rejected` | admin | reason code (`document_unreadable`, `name_mismatch`, `licence_expired`, `cnic_check_failed`, `other`) | audit; driver sees reason code text |
| `approved` → `suspended` | admin (report, incident) or system (licence/registration expired) | reason code | future rides cancelled as in §5.2; audit |
| `suspended` → `under_review` | admin | — | audit |

Account suspension (§5) overrides everything: a suspended account can't drive
whatever its profile says. Every transition is a row-locked `UPDATE … WHERE
status = :expected` so two admins can't decide the same case twice.

### 6.2 What each check means (process, to be confirmed by Maaz)

| Check | Proposed process | Status of this proposal |
|---|---|---|
| CNIC identity | Either (a) NADRA Verisys (or an approved KYC vendor) lookup of CNIC + name, **or** (b) manual: reviewer compares the CNIC images with the registered name and a live selfie | Vendor, cost, contract and legal basis **not researched or verified**. (b) only shows that the documents look consistent, not that they are genuine. **Decision D2.** |
| Driving licence | Reviewer checks name/CNIC match, category (LTV/motor car), expiry; uses the issuing authority's online verification where one exists | Which authorities offer online verification, and its terms, **not verified** |
| Vehicle registration | Reviewer checks plate, make/model, owner name; uses the provincial Excise & Taxation online lookup where available; if the owner ≠ driver, an authorisation letter | **Not verified** |
| Manual review | Reviewer ≠ driver; checklist items all ticked; decision + checklist in the audit log | Designed here |

**Explicitly NOT verified, even when `approved`:** criminal record or
background; driving history and points; insurance; roadworthiness/fitness of
the vehicle; that the person driving on the day is the approved driver; that
the vehicle used is the approved one; medical fitness; conduct. The UI and
terms must say so (§10).

### 6.3 Enforcement rollout

1. Ship tables + submission + admin review with `DRIVER_ELIGIBILITY_ENFORCED=false`; UI shows "Driver documents not verified" for everyone not approved.
2. Backlog of existing drivers reviewed.
3. Turn the flag on (env change, approval gate): `create_ride` and `respond_to_booking` (accept) return 403 `Driver approval required` unless approved + approved vehicle. Already-published rides by unapproved drivers: **decision D4** (cancel, or let them run out).

## 7. Vehicle information (migration M5-C)

`vehicles`: `id`, `driver_id` FK users, `make` (≤ 40), `model` (≤ 40),
`year` (SMALLINT, CHECK 1980–2100), `colour` (≤ 30), `plate_number` (≤ 16,
normalised uppercase alphanumeric + hyphen), `seats_total` (SMALLINT, CHECK
1–8), `status` (`pending`/`approved`/`rejected`/`retired`, VARCHAR + CHECK),
`registration_doc_ref` (opaque storage key, nullable), `reviewed_by` FK users
NULL, `reviewed_at`, `created_at`. Partial unique index on `plate_number WHERE
status IN ('pending','approved')`.

Later: `rides.vehicle_id` FK NULL (nullable forever for legacy rides);
`available_seats ≤ vehicles.seats_total - 1` enforced in the service, not a
constraint (legacy rows).

Visibility: make/model/colour shown on ride details; **plate only to the
passenger after acceptance** and to admins. Document refs never leave the
admin API; images through signed URLs (≤ 5 min) from a private bucket.
Storage provider and encryption: **decision D3** (AST-024).

## 8. User reports and moderation (migration M5-D)

`reports`: `id`, `reporter_id`, `subject_user_id`, `ride_id` NULL, `booking_id`
NULL, `category` (`safety_incident`, `harassment`, `no_show`, `unsafe_driving`,
`fraud_or_payment`, `fake_profile`, `other`), `description` (≤ 2000), `status`
(`open` → `triaged` → `actioned` | `dismissed`), `severity` (`low`/`high`/`urgent`,
set at triage; `safety_incident` defaults to `urgent`), `assigned_admin_id`,
`resolution_code`, `created_at`, `resolved_at`.
`blocks`: `(blocker_id, blocked_id)` PK, `created_at`.

Workflow:

1. `POST /reports` by an authenticated user who has an **accepted** booking on a ride with the subject (as driver or passenger), or within 7 days after it. Not self. Rate limit e.g. 5/day. Response: 201 with a fixed message; the subject is never told who reported.
2. `safety_incident` shows a fixed banner before submission: in danger, call emergency services (15 police, 1122 rescue) first; RideShare is not an emergency service.
3. Admin queue sorted by severity then age. Triage sets severity and assignment.
4. Actions: warn (note only), suspend account (§5), suspend driver profile (§6), dismiss. Every action references the report id in the audit row.
5. Reports never auto-suspend (T7). Repeated reports surface as a count on the admin user view.
6. Blocking: hides each other's rides in search and rejects new booking requests between them; existing accepted bookings stay (cancel through normal flows).

Retention: **decision D7** (proposed: closed reports 2 years, then description redacted, metadata kept).

## 9. Append-only admin audit log (migration M5-B)

`admin_audit_log`: `id BIGSERIAL`, `at TIMESTAMPTZ NOT NULL DEFAULT now()`,
`actor_user_id` FK users NULL (NULL = operator script/system job),
`action VARCHAR(64)`, `target_type VARCHAR(32)`, `target_id BIGINT`,
`reason_code VARCHAR(64) NULL`, `note VARCHAR(500) NULL`, `request_id
VARCHAR(64) NULL`, `details JSONB NULL` (before/after of **non-PII** fields
only; masked values where PII is involved). Index `(target_type, target_id,
at DESC)` and `(actor_user_id, at DESC)`.

Append-only, in three layers:

1. **Application:** the only code path is `audit.record(db, …)`, called inside the caller's transaction (no separate commit), so the action and its audit row commit or roll back together.
2. **Database trigger:** `BEFORE UPDATE OR DELETE ON admin_audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()`, which raises. `TRUNCATE` is blocked with a statement trigger.
3. **Grants (later, approval gate):** the app's DB role gets `INSERT, SELECT` only; the migration role owns the table. On Neon this is a production role change.

Optional hardening: `prev_hash`/`row_hash` chain for tamper evidence, and a
periodic export to separate storage. The log itself never stores full
CNIC/phone; a PII reveal is logged as `action = pii_revealed`, `details =
{"fields": ["phone_number"]}`.

## 10. Honest UI labels and privacy

### 10.1 Labels

| Situation | Label (exact) | Never |
|---|---|---|
| Any driver today (and any driver not `approved`) | **Driver documents not verified** | "Verified", tick icons, "Trusted driver" |
| `approved` | **Documents checked by RideShare** + date, with a "What we check" link listing §6.2 and the NOT-verified list | "Verified driver", "Background checked", "Safe" |
| `suspended` driver | ride not shown | — |
| Account page | **Details you provided** (AST-004 copy) | "verified by RideShare" (still on v2) |
| Vehicle not approved | **Vehicle details provided by the driver** | — |

`verification.DRIVER_NOT_VERIFIED_LABEL` holds the first string; the frontend
should use the same text.

**Why `verification_status` is not in ride responses yet:**
`tests/test_frontend_reads.py::test_details_include_history_and_require_authentication`
pins the exact key set of `RideResponse`, and the passenger/driver UI branches
are being built against it. An always-`not_verified` field adds no information
the UI can't show as a constant label. When `driver_profiles` exists, add
`driver_verification_status` to `RideResponse` as an additive field in one
coordinated PR that updates that test and `frontend/src/api/types.ts`.
`tests/test_trust_privacy.py` currently fails if any response model gains a
`verif*`/`badge`/`trusted` property; that PR replaces the check with one that
pins the allowed values.

### 10.2 Who can see CNIC and phone

| Viewer | CNIC | Phone |
|---|---|---|
| The owner | Masked (`*****-****567-*`); full value never returned (they typed it) | Full (own number); needed for edit later |
| Other passengers/drivers | Never | Never before acceptance. After acceptance: **decision D9**: in-app relay (recommended) or reveal the number to the counterpart from acceptance until 24 h after departure |
| Admin, normal views | Masked | Masked |
| Admin, explicit reveal | Full, reason required, audited, rate-limited | Full, same |
| Logs, errors, audit log, analytics | Never | Never |

### 10.3 Masking rules (implemented in `privacy.py`)

- **CNIC:** `*****-****NNN-*`, the last three digits of the middle block only. The first five digits (issuing region) and the **last digit, which encodes gender** (odd male, even female), are always hidden. This deliberately differs from the CNIC doc's draft example `*****-*******-1`, which would reveal gender.
- **Phone:** `+92 3** *** **NN`, the last two digits only.
- **Fail closed:** a value that isn't a recognisable CNIC/PK mobile (legacy rows were stored unvalidated) is masked entirely (`*****-*******-*`, `+92 *** *** ****`), never partially echoed. Only ASCII digits count.

### 10.4 Retention (proposals, **decision D7**)

Rejected driver documents: delete images 30 days after the decision. Approved:
keep while approved + 1 year after the profile closes. Deactivated account:
redact name/phone, keep the CNIC blind index (ban evasion) and the audit
rows. Needs a privacy notice and legal review (AST-029).

## 11. Schema proposals and migration order

**Order.** Wave 1 chain first: `d924a6e38f10` (on v2) → `e3a7c41b9f52`
(AST-003, `perf/fk-listing-indexes`) → `f6b2d8e0c413` (AST-005,
`fix/email-case-identity`). Every M5 revision has `down_revision` chained after
`f6b2d8e0c413`, or after whichever M4 schema revision (expiry, cancellation
record, multi-seat, idempotency, lifecycle events) has merged by then. Revision
ids are generated when written; rebase the chain, never branch it (one head).

| # | Revision (proposed) | Contents | Legacy safety | Lock/runtime | Downgrade |
|---|---|---|---|---|---|
| M5-A | `account_status_token_version` | `users.status` + CHECK, `users.token_version`, `users.status_changed_at` | Server defaults fill every row; CHECK always true | PG ≥ 11: adding a column with a constant default is metadata-only; short `ACCESS EXCLUSIVE` | Drop the three columns. Code must already tolerate their absence (deploy the code rollback first) |
| M5-B | `admin_audit_log` | table, indexes, immutability trigger + function | New table | Instant | Drop trigger, function, table. **Loses audit history**: export first (`COPY … TO`) |
| M5-C | `driver_profiles_vehicles` | `driver_profiles` (`user_id` PK/FK, `status` + CHECK, `licence_number`, `licence_expiry`, `submitted_at`, `reviewer_id`, `decided_at`, `decision_checklist JSONB`, `rejection_reason`), `vehicles` (§7), partial unique plate index | New tables; no row = `not_verified` | Instant (empty tables, so a normal `CREATE INDEX` is fine) | Drop tables (document refs in storage must be cleaned separately) |
| M5-D | `reports_blocks` | `reports`, `blocks`, indexes | New tables | Instant | Drop tables (export first if any real reports exist) |
| later | `rides_vehicle_id` | `rides.vehicle_id` FK NULL; index created `CONCURRENTLY` in an autocommit block | Nullable | `ADD COLUMN` instant; index concurrent | Drop index, drop column |
| later | AST-030 PII columns | CNIC doc §4–6 | — | — | CNIC doc §7 |

Rollout per revision: (1) expand migration, deployed with code that ignores
the new objects; (2) code that writes them behind a flag; (3) flag on. Each
revision gets an upgrade → downgrade → re-upgrade test in
`tests/test_migrations.py` like the existing ones, plus a seeded legacy-row
test for M5-A. Pre-flight for M5-A on production (counts only, **not approved
to run**): `SELECT count(*) FROM users;` and confirm that no column named
`status`/`token_version` already exists via `information_schema.columns`.
Every staging/production `alembic upgrade` is approval-gated.

## 12. Delivery split

**(a) Done on `feat/trust-safety-foundation` (code only):** `require_admin`,
`admin_router`, the route-inventory test, CNIC/phone masks, the response
privacy guard, the `verification` module and the "no verification claim"
guard.

**(b) Next, code + migration, each its own PR after Wave 1 merges:** M5-A
suspension + token version (AST-013) → M5-B audit log → first admin endpoints
(user lookup, suspend/reinstate) → M5-C driver profiles + vehicles + review
queue (AST-024/025/035) → M5-D reports + blocks (AST-026) → enforcement flags.

**(c) Needs Maaz:** decisions D1–D10 in `handover/tasks/M5-trust.md`.
