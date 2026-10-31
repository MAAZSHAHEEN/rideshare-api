# Dependency audit, 31 Oct 2026 (AST-008)

Branch `chore/dependency-security`, base `rideshare-v2` @ e8da425.
Tools: pip-audit 2.10.1 (PyPI/OSV data as of 31 Oct 2026, 01:00–02:30 PKT), npm 10.9.9 / Node 22.23.3.
Read-only audit; nothing was deployed and no production system was touched.

## Summary

| | Before | After |
|---|---|---|
| Python: pip-audit records | 53 (in 11 packages) | **0** |
| Python: unique advisories | 29 | **0** |
| Python: pinned packages | 35 | 26 |
| npm (`frontend/`, all severities, incl. dev) | 0 | 0 (no npm changes) |
| Active exceptions | n/a | **0** |

pip-audit lists most advisories twice (once under the PYSEC id, once under an alias), so 53 records are 29 distinct advisories.

How the 29 were cleared:

| Method | Advisories | Packages |
|---|---|---|
| Removed: unused orphan pin | 6 | cryptography (plus cffi, pycparser, colorama, psycopg2-binary, which had no advisories) |
| Removed: python-jose → PyJWT migration | 7 | python-jose 1, ecdsa 2, pyasn1 4 (rsa and six also dropped) |
| Upgraded, minimum fixed version | 16 | anyio 2, dnspython 1, idna 1, Mako 1, python-dotenv 1, starlette 5, python-multipart 5 |

## Reachability triage

"Direct" means imported by app code; "transitive" means pulled in by another package. `requirements.txt` is a full freeze, so every row was pinned there. Reachability comes from grepping the app (`main.py`, `config.py`, `database.py`, `auth_rate_limit.py`, `frontend_serving.py`, `routers/`, `services/`, `alembic/`) plus reading the library call path.

| Package (before → after) | Advisory | Fix | Dep | Reachable in this app? | Action |
|---|---|---|---|---|---|
| anyio 4.12.1 → 4.14.2 | PYSEC-2026-4025 / CVE-2026-63374 (IDNA TLS hostname confusion in `connect_tcp`/`TLSStream.wrap`) | 4.14.2 | transitive (starlette) | No. The app makes no outbound TLS through anyio; asyncpg and redis-py use asyncio directly. | Upgraded |
| anyio | PYSEC-2026-4024 / CVE-2026-64847 (process-pool stderr pipe wedge) | 4.14.2 | transitive | No. `anyio.to_process` is not used. | Upgraded |
| cryptography 46.0.5 → removed | PYSEC-2026-35, PYSEC-2026-36, PYSEC-2026-3552, -3553, -3554, GHSA-537c-gmf6-5ccf (X.509 name constraints, buffer overflow, PKCS7 oracle, chain blow-up, bundled OpenSSL) | 46.0.6 to 50.0.0 | orphan pin | No. Nothing imports it and no installed package requires it (`pipdeptree -r`). HS* JWTs need no crypto backend. | Removed |
| dnspython 2.8.0 → 2.9.0 | CVE-2026-114605 (DNS name decompression CPU) | 2.9.0 | transitive (email-validator) | No. pydantic `EmailStr` calls email-validator with `check_deliverability=False`, so no DNS queries are made. | Upgraded |
| ecdsa 0.19.1 → removed | PYSEC-2026-1325 / CVE-2024-23342 (Minerva timing on signing, **no fix**) | none | transitive (python-jose) | No. No EC keys; HS* only. | Removed with python-jose |
| ecdsa | PYSEC-2026-2467 / CVE-2026-33936 (DER parsing) | 0.19.2 | transitive | No. | Removed |
| idna 3.11 → 3.15 | PYSEC-2026-215 / CVE-2026-45409 (`idna.encode` CPU DoS, incomplete fix for CVE-2024-3651) | 3.15 | transitive (email-validator, anyio, httpx) | **Potentially, pre-auth.** `/auth/register` and `/auth/login` validate `EmailStr`; for a non-ASCII domain, email-validator calls `idna.alabel()` on each label. Input is capped by pydantic's 2048-character email limit, which bounds but does not eliminate the cost. | Upgraded |
| Mako 1.3.10 → 1.3.12 | PYSEC-2026-2617 / CVE-2026-44307 (Windows backslash traversal) | 1.3.12 | transitive (alembic) | No. Linux only, and Alembic uses Mako only to render new revision files at dev time. | Upgraded |
| pyasn1 0.6.2 → removed | PYSEC-2026-2263, -3455, -3456, -3457 (BER/DER decoder DoS) | 0.6.3 / 0.6.4 | transitive (python-jose, rsa) | No. No ASN.1 or RSA key parsing with HS*. | Removed with python-jose |
| python-dotenv 1.2.1 → 1.2.2 | PYSEC-2026-2270 / CVE-2026-28684 (`set_key`/`unset_key` symlink follow) | 1.2.2 | direct | No. Only `load_dotenv()` is called. | Upgraded |
| python-jose 3.5.0 → removed | PYSEC-2026-4298 / CVE-2026-85394 (HMAC key confusion with DER public keys, **no fix**) | none | direct | No. The key is an HMAC secret, and `jwt.decode` pins `algorithms=[ALGORITHM]` (HS256/384/512 enforced in `config.py`). The issue is unfixed and the project is poorly maintained, so we migrated anyway. | Replaced with PyJWT 2.15.1 |
| python-multipart 0.0.22 → 0.0.31 | PYSEC-2026-3036, -3037, -3038, -3039, -3040 (multipart/urlencoded parser DoS and parser differential) | 0.0.26 to 0.0.31 | transitive (`fastapi[standard]`) | No. FastAPI runs the form parser only for `Form`/`File` parameters, and every endpoint takes JSON. | Upgraded |
| starlette 0.52.1 → 1.3.1 | PYSEC-2026-161 / CVE-2026-48710 (Host header injected into `request.url`) | 1.0.1 | direct (`StaticFiles`, `JSONResponse`, `run_in_threadpool`) | **Limited.** App code never reads `request.url`; rate limiting uses `scope["path"]` and `scope["client"]`. The only reach is Starlette's `redirect_slashes` 307 `Location` header (e.g. `GET /rides`), built from the Host header the caller sent. Low impact. | Upgraded |
| starlette | PYSEC-2026-248 / CVE-2026-54282 (non-`/` path moves the `request.url` authority) | 1.3.0 | direct | **Limited**, same as above. | Upgraded |
| starlette | PYSEC-2026-249 / CVE-2026-54283 (urlencoded `max_fields`/`max_part_size` ignored) | 1.3.1 | direct | No. There are no form endpoints. | Upgraded |
| starlette | PYSEC-2026-2280 / CVE-2026-48817 (`HTTPEndpoint` arbitrary method dispatch) | 1.1.0 | direct | No. `HTTPEndpoint` is not used. | Upgraded |
| starlette | PYSEC-2026-2281 / CVE-2026-48818 (`StaticFiles` UNC SSRF on Windows) | 1.1.0 | direct | No. The app runs on Linux (`/assets` uses `StaticFiles`). | Upgraded |

Totals: 1 potentially reachable (idna), 2 with limited, low-impact reach (starlette Host/URL), 26 unreachable. All 29 are cleared.

## Upgrade groups (one commit each, full backend suite after each)

| # | Commit | Change | Backend suite |
|---|---|---|---|
| 0 | `7383d9f` | `requirements.txt` UTF-16LE+BOM+CRLF → UTF-8+LF. Content identical after decoding, verified with `diff`. | (no dependency change) |
| 1 | `b76d15b` | Drop unused orphans: cryptography, cffi, pycparser, colorama, psycopg2-binary | 194/194 OK |
| 2 | `41d950f` | anyio 4.14.2, idna 3.15, dnspython 2.9.0, Mako 1.3.12, python-dotenv 1.2.2 | 194/194 OK |
| 3 | `2576ad8` | starlette 1.3.1, fastapi 0.136.3, python-multipart 0.0.31 | 203/203 OK (includes the 9 new audit-gate tests) |
| 4 | `4873712` | python-jose → PyJWT 2.15.1; drop ecdsa, rsa, pyasn1, six | 210/210 OK (+7 JWT compatibility tests) |
| 5 | `1d64c90` | CI dependency-audit job, Dependabot, exceptions file, gate script + tests | (final run in the PR body) |

Each group rebuilt a fresh venv from `requirements.txt` alone, followed by `pip check` (clean every time).

### Why fastapi 0.136.3 and not the latest

The starlette fixes need starlette ≥ 1.3.1. FastAPI 0.131–0.132 cap starlette below 1.0; 0.133 lifts the cap. **FastAPI 0.137.0 restructured `include_router`**: `app.routes` now holds `_IncludedRouter` objects instead of flattened `APIRoute`s. On 0.138.2 the running app served every route correctly (checked by hand: `/rides/` 401, `/rides` 307, `/rides/me` 401, `/bookings/me` 401), but two test harnesses that introspect `app.routes` failed:
- `test_frontend_reads.FrontendOpenAPITests.test_new_read_contracts_and_me_schema`: AttributeError, `_IncludedRouter` has no `.path`.
- `test_frontend_serving...test_actual_api_routes_and_docs_remain_accessible`: `/rides/` gave 404 because the harness copies only `APIRoute` objects.

0.136.3 is the newest release before that change. It clears every advisory and needs no test changes. **Moving to FastAPI ≥ 0.137 is a separate task** that has to update those two harnesses. Expect Dependabot to propose it.

## python-jose → PyJWT

The migration was justified even though the jose advisory is unreachable here:
- PYSEC-2026-4298 has no fixed release, and python-jose is poorly maintained (this is the second incomplete fix for the same key-confusion class).
- It pulled in 7 more advisories (ecdsa ×2, one of them unfixable; pyasn1 ×4).
- The swap is small and fully covered by tests.

Behaviour kept identical:
- **Encode:** same header `{"alg","typ"}`, same claims `sub, role, iat, exp`, and the same `ACCESS_TOKEN_EXPIRE_MINUTES` expiry. PyJWT output is **byte-identical** to python-jose for HS256/384/512 (`tests/test_jwt_compat.py::test_pyjwt_encoding_is_byte_identical_to_jose`).
- **Decode:** algorithm still pinned to config, signature and `exp` verified, `sub`/`role`/`exp` required, and every hand-written check (sub format and range, role enum, int `exp` > now, optional `iat` sanity, optional `nbf`) kept verbatim in `routers/dependencies.decode_access_token()`. That function was extracted so it can be tested without a database. `get_current_user` still returns a generic 401 with `WWW-Authenticate: Bearer`.
- **Old tokens:** frozen tokens generated with python-jose 3.5.0 (current format, plus the pre-hardening format without `iat`) verify under PyJWT for all three algorithms. The tokens use a fake, test-only key and expire in 2038.
- **Differences:** PyJWT also rejects a future `iat` (`ImmatureSignatureError`). The app already rejected that, so the result is the same. A token carrying `aud` is rejected by both libraries when no audience is configured. Our issuer never sets `aud`.
- **Rollout and rollback:** no token invalidation, no secret or env change. Rollback is a revert of `4873712`. Tokens issued by either version verify under the other.

## CI gate and exceptions

- Job `dependency-audit` in `.github/workflows/ci.yml` (the existing `test` job is unchanged):
  - `pip-audit==2.10.1 --requirement requirements.txt --strict` fails on **any** unexcepted Python advisory. pip-audit's data carries no reliable severity, so a high/critical-only threshold can't be applied honestly on the Python side.
  - `npm audit --json`, gated by `.github/scripts/audit_gate.py`, fails on unexcepted **high/critical** only. It fails closed if npm returns an error instead of a report.
- Exceptions go in `.github/audit-exceptions.toml`. Each entry needs `id`, `package`, `reason` and `review_by`. An entry past its `review_by` date fails CI.
- **Current exceptions: none.** Nothing needed one: every advisory was fixed or its package removed.
- Dependabot (`.github/dependabot.yml`): pip `/`, npm `/frontend` and github-actions, weekly on Monday 06:00 PKT, minor+patch grouped per ecosystem, targeting `rideshare-v2`. Majors arrive as individual PRs. GitHub's separate "Dependabot security updates" setting is a repo setting and was not changed.

## Review schedule

| Item | Review by | Why |
|---|---|---|
| FastAPI ≥ 0.137 upgrade (test-harness update) | 2026-11-30 | Stay within supported FastAPI releases. |
| Re-run this audit and refresh this document | 2026-12-01 | The CI job runs on every PR; this is the human review. |
| Any future exception | its `review_by` (≤ 60 days recommended) | Enforced by CI. |

## Not covered

- The Docker base image and OS packages (no container scan was run).
- The FastAPI Cloud runtime image (it installs from `requirements.txt`; its base layer was not audited).
- Licences.
- Advisory databases change daily, so "0 known" is as of 31 Oct 2026.
