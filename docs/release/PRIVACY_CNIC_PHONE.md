# CNIC and phone numbers at rest: risk and encryption migration plan

Prepared 31 Oct 2026 (staging-ops). Builds on `docs/privacy/CNIC_PHONE_PROTECTION.md` (PR #8, the
detailed cryptographic design) and SECURITY_REVIEW_RC §1 (evidence of what the RC already masks).
This file adds the legal context, the risk call for launch, the concrete Alembic revision plan after
`d5e1a0b00002`, rollback and effort. **Not legal advice**; the legal summary is from the sources cited.
Launch blocker LB-10.

## 1. Current state (RC `52e266b`)

- `users.cnic` (`VARCHAR`, NOT NULL, UNIQUE `users_cnic_key`) and `users.phone_number` (NOT NULL)
  are **plaintext** in PostgreSQL, in every Neon branch, PITR history and any `pg_dump`.
- New input is normalised (13-digit CNIC, `+923XXXXXXXXX`); legacy rows are verbatim.
- No API response returns either value; the admin view returns masks only (`privacy.mask_cnic`,
  `mask_phone`) and audits each view; logs and 422 bodies are tested to be free of them.
- So the exposure is **at rest**: anyone with a DB credential, a Neon console session, a branch or a
  dump reads every CNIC and phone number.

## 2. Legal context (Pakistan, as of Oct 2026)

| Instrument | Status | Relevance |
|---|---|---|
| **PECA 2016**, s.16 "unauthorised use of identity information" | in force | "Whoever obtains, sells, possesses, transmits or uses another person's identity information without authorization shall be punished with imprisonment … up to three years or with fine … up to five million rupees, or with both." CNIC is identity information. A breach that leaks CNICs exposes the people who misuse them, and puts the platform in the position of having failed to protect them. PECA has **no breach-notification duty and no security standard** for data holders. Source: DLA Piper, *Data protection laws in Pakistan* (last modified 17 Mar 2026), https://www.dlapiperdataprotection.com/?c=PK |
| **PECA (Amendment) Act 2025** | in force | creates SMPRA, NCCIA, Social Media Protection Tribunal; aimed at online content/platforms, not a data-protection regime (same source) |
| **Personal Data Protection Bill** (2023 draft; 2025/2026 drafts) | **not enacted** | MoITT's Year Book 2024-25 says the PDPB "has been updated … cleared by the Inter-Ministerial Committee and Cabinet" and is under legal vetting (https://moitt.gov.pk/SiteImage/Publications/YEAR%20BOOK%202024-25.pdf, search excerpt). The draft lists **"computerized national identity card or passport" as sensitive personal data**, requires explicit consent for sensitive data, 72-hour breach notification to a Commission, and local processing for "critical" data (DLA Piper, above; Lex Mundi guide https://www.lexmundi.com/guides/data-privacy-guide/jurisdictions/asia-pacific/pakistan/, search excerpt). Secondary sources from Mar–May 2026 confirm it is still not law (e.g. https://www.recordinglaw.com/world-laws/world-data-privacy-laws/pakistan-data-privacy-laws, search excerpt). DLA Piper's page was read in full on 31 Oct 2026; the three marked "search excerpt" were not opened in full.. |

Takeaway: no statute today *requires* encryption at rest, but the likely incoming law classes CNIC
as sensitive data, and a CNIC leak is the kind of harm PECA criminalises for whoever misuses it. The
reputational and user-harm case is stronger than the compliance case.

## 3. Risk call for launch

| Option | What | Effort | Residual risk |
|---|---|---|---|
| **0. Stop collecting** (data minimisation) | make CNIC optional or drop it from registration until a verification feature needs it; keep phone | S (½ d) + product decision | lowest; existing rows still need §4 or deletion |
| **A. Accept for MVP with compensating controls** (written acceptance by Maaz) | app role not owner/superuser; Neon access limited to Maaz; no prod-data branches except sanitized (NEON_STAGING_PLAN); dumps encrypted (BACKUP_RESTORE_TEST §4); SECRET/DB rotation per SECRET_ROTATION_CHECKLIST | S | a DB credential or console compromise exposes all CNICs/phones |
| **B. Encrypt before launch** (§4) | envelope encryption + blind index | **L, 5–8 dev days** + review + staged rollout over ≥ 2 weeks | a DB-only compromise exposes nothing readable; app + key compromise still does |

Recommendation: **A at launch with a dated commitment to B**, or 0 if the product does not use CNIC
yet (nothing in the RC reads CNIC except the duplicate check at registration). Decision is Maaz's.

## 4. Encryption migration plan (B)

Design (unchanged from CNIC_PHONE_PROTECTION §4): AES-256-GCM ciphertext columns + HMAC-SHA256 blind
indexes over the canonical value, `PII_MODE` flag `plaintext → dual → encrypted → encrypted_only`.

**Envelope encryption.** A per-version 256-bit data key (DEK) encrypts values. The DEK is stored
**wrapped** (encrypted) by a key-encryption key (KEK):
- now: KEK in the FastAPI Cloud secret store (`PII_KEK_<v>`), wrapped DEKs in a small `pii_keys(version
  smallint pk, wrapped_dek bytea, created_at, retired_at)` table — rotating the KEK re-wraps a few
  rows instead of re-encrypting every user;
- later: KEK in a cloud KMS (AWS KMS / GCP KMS); the app calls `Decrypt` on the wrapped DEK at startup
  and keeps the plaintext DEK only in memory.
- Blind-index keys (`PII_BIDX_KEY_CNIC`, `PII_BIDX_KEY_PHONE`) are separate secrets, never wrapped by
  the same KEK, never truncated. CNIC space ~10¹³: bidx key + DB leak ⇒ brute-forceable, so bidx keys
  are tier-0 secrets.
- AAD binds each ciphertext to table/column/key version; random 96-bit nonce per value.
- Offline escrow of KEK + bidx keys before the contract phase (losing them after contract = permanent
  loss of CNIC/phone).

**Alembic revisions after `d5e1a0b00002`** (ids indicative; chain stays linear):

| Revision | Phase | Change | Lock / safety | Downgrade |
|---|---|---|---|---|
| `e1f0a0b00001` | expand | `pii_keys` table; nullable `users.cnic_ct`, `cnic_bidx`, `phone_ct`, `phone_bidx`, `pii_key_version` | metadata-only `ADD COLUMN`, `lock_timeout 5s` | drop columns/table — lossless (plaintext authoritative) |
| `e1f0a0b00002` | expand | `CREATE UNIQUE INDEX CONCURRENTLY uq_users_cnic_bidx ON users (cnic_bidx) WHERE cnic_bidx IS NOT NULL`; `ix_users_phone_bidx` concurrently | autocommit block, INVALID-index repair like `e3a7` | drop concurrently |
| — (code) | dual write | deploy with `PII_MODE=dual`; register writes plaintext + ct + bidx | | `PII_MODE=plaintext` |
| — (job) | backfill | `python -m jobs.backfill_pii` (new): batches of 500 by id, `WHERE cnic_bidx IS NULL`, idempotent, advisory-locked like #26, reports ids of non-canonical or colliding rows (no values) | short transactions, SKIP LOCKED | leave columns, set mode back |
| — (code) | switch reads | `PII_MODE=encrypted`: duplicate check via `cnic_bidx` with legacy fallback for NULL bidx; soak ≥ 7 days | | `PII_MODE=dual` (instant) |
| `e1f0a0b00003` | contract 1 | `ALTER COLUMN cnic/phone_number DROP NOT NULL`; app `PII_MODE=encrypted_only` stops writing plaintext | | re-add NOT NULL only if no NULLs |
| — (job, approval) | contract 2 | `UPDATE users SET cnic = NULL, phone_number = NULL WHERE cnic_ct IS NOT NULL` in batches, after a fresh verified backup | **irreversible without keys or backup** | decrypt-back script from ct, or restore |
| `e1f0a0b00004` | contract 3 | drop `users_cnic_key`, drop `cnic`, `phone_number` columns | irreversible | restore only |

Deploy order: each expand revision **before** the code that uses it (same rule as §6 of
MIGRATION_READINESS); contract revisions **after** the code no longer reads plaintext.

**Rollback summary:** up to and including "switch reads" everything is reversible by flipping
`PII_MODE` (plaintext still present). Contract 2 onward needs the keys (decrypt-back) or the
pre-contract backup. Staging rehearsal on a **sanitized** branch first (synthetic CNICs).

**Effort:** `pii.py` + key loading + tests (2 d); migrations e1f0…01/02 + tests (1 d); dual-write and
lookup changes in register + admin mask path (1 d); backfill job + tests + report (1–2 d); staging
rehearsal + docs (1 d); contract phase later (½ d + approvals). **≈ 6–8 dev days**, plus Codex Astra
review and Maaz approvals for keys (secret change), each migration, and the contract step.

**Tests the implementation must carry:** CNIC_PHONE_PROTECTION §5 (round-trip, tamper, AAD swap,
key rotation, mode transitions, duplicate detection via bidx and fallback, migration up/down/up,
registration race on the unique bidx index, logs free of values) + a test that a `pg_dump` of a
backfilled DB contains no 13-digit CNIC strings.
