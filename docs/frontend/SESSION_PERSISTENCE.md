# Session persistence (AST-023, Milestone 2)

Status: implemented on `feat/passenger-journey`. MVP decision; revisit with the
backend before payments, admin or long-lived sessions.

## Problem

Until now the SPA kept the JWT bearer token only in memory. Every reload, deep
link or accidental refresh signed the passenger out, which broke the core
journey (search → details → request → My bookings) and made "return to the ride
after signing in" impossible.

## Existing architecture (unchanged)

- `POST /auth/login` returns `{access_token, token_type: "bearer"}`; a signed
  HS256 JWT with `sub`, `role`, `iat`, `exp` (`ACCESS_TOKEN_EXPIRE_MINUTES`,
  default 60). No refresh token, no revocation list, no cookie.
- Every protected call sends `Authorization: Bearer <token>`; the client uses
  `credentials: 'omit'`, so the browser never attaches ambient credentials.
- CORS: `allow_credentials=False`, explicit origins (`FRONTEND_ORIGINS`).
- CSP from PR #6 (AST-006) on the SPA: `script-src 'self'`, `connect-src 'self'`,
  `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`.
- Identity and capabilities always come from `GET /me`; the JWT payload is used
  only as an expiry timer.

## Options considered

| Option | Survives reload | XSS exposure of the token | CSRF | Backend change |
|---|---|---|---|---|
| A. Memory only (before) | No | Live session only | None (bearer) | None |
| **B. `sessionStorage`, tab-scoped (chosen)** | Yes, same tab | Readable by injected script for ≤ token lifetime | None (bearer, not ambient) | None |
| C. `localStorage` | Yes, all tabs, after browser restart | Readable by injected script; persists after the tab closes | None | None |
| D. httpOnly `SameSite=Lax` cookie + CSRF token | Yes | Not readable by script (script can still act as the user while the page is open) | Needs CSRF token / double-submit, `allow_credentials=True`, cookie logout | Yes: login sets cookie, auth reads cookie, CSRF middleware, logout endpoint, CORS credentials |
| E. Memory access token + httpOnly refresh cookie (rotation) | Yes | Short-lived access token only | Refresh endpoint needs CSRF protection | Yes: refresh table/rotation, revocation |

## Decision

**B: `sessionStorage`, tab-scoped, re-verified on every restore.**

D is the better end state (the roadmap's AST-023 card recommends it for a
same-origin deployment) but it is a backend contract change touching
`routers/auth.py`, `routers/dependencies.py`, CORS and every client call. The
sprint rule is to build the passenger UI on the existing endpoints and route
contract changes through the lead, so D/E are documented below as the follow-up.

### What is stored

- Key `rideshare.session.v1` in `window.sessionStorage` only. Value:
  `{ token, expiresAt, mode }`. `mode` is the presentation-only travel mode.
- Never stored: the profile (name, email, role), CNIC, phone, passwords, any
  API response. `localStorage` is never written (asserted in Vitest and
  Playwright).

### Rules (frontend/src/auth/sessionStore.ts, AuthProvider.tsx)

1. Written only after a successful login **and** a valid `GET /me`.
2. On load, the stored value is parsed defensively. Malformed JSON, a non-string
   or oversized token, or a token whose own `exp` has passed is deleted without
   any network call. The stored `expiresAt` is ignored; expiry is re-derived from
   the token so editing storage cannot extend it.
3. A stored token is never trusted on its own: the app shows "Restoring your
   session…" and calls `GET /me` with it. Only a valid identity restores the
   session; 401 → cleared with "Your session has ended"; network/5xx → cleared
   with "We couldn't restore your session" (fail closed).
4. Any protected 401 (existing generation check), the JWT expiry timer, focus or
   visibility checks, and logout all clear the key and the TanStack Query cache.
5. Tab scope: a new tab or window starts signed out (sessionStorage is
   per-tab), and the data disappears when the tab closes. Duplicating a tab
   copies it (browser behaviour); that copy expires with the token.
6. Return path: when a guard sends a visitor to `/login`, it passes the current
   in-app path (`/passenger?ride=7`). `safeReturnPath` accepts only same-origin
   `/passenger`, `/driver`, `/account` paths (rejects `//host`, backslashes,
   absolute URLs, `javascript:`). A deliberate logout keeps no return path.

## Trade-offs

**XSS.** Any script running in the page can read `sessionStorage`, so an XSS bug
would let an attacker exfiltrate the bearer token and use it elsewhere until it
expires (≤ 60 min by default; no server-side revocation exists). With memory-only
storage the same attacker could still call the API as the user while the page
was open, so the marginal risk is the off-page replay window. Mitigations in
place: strict CSP (no inline or third-party script, `connect-src 'self'`; PR #6),
React's escaping, no `dangerouslySetInnerHTML`, no third-party scripts, short
token lifetime, tab scope, and nothing except the token is stored. httpOnly
cookies (D) remove token exfiltration but not in-page abuse.

**CSRF.** None introduced: the token is sent only by our code in the
`Authorization` header, never as an ambient credential; CORS keeps
`allow_credentials=False`. Option D would introduce CSRF exposure and need a
CSRF token.

**Shared computers.** Closing the tab or logging out removes the token; there is
no "remember me". This is deliberately weaker persistence than `localStorage`.

## Follow-up (needs backend, coordinate via the lead)

- Move to D or E: httpOnly `Secure; SameSite=Lax` cookie (`__Host-` prefix when
  same-origin), CSRF double-submit header, `POST /auth/logout`, CORS
  `allow_credentials=True` for the exact origins, cookie-aware `get_current_user`.
- Token revocation (`jti` deny-list or `token_version` on users) so logout and
  password reset invalidate stolen tokens server-side.
- Keep `ACCESS_TOKEN_EXPIRE_MINUTES` short (≤ 60) while tokens are script-readable.

## Rollback

Revert `frontend/src/auth/sessionStore.ts` usage in `AuthProvider.tsx` (remove
`saveSession`/`loadSession`); the app returns to memory-only sessions. Old keys
left in a tab are harmless and are removed on the next logout/expiry.
