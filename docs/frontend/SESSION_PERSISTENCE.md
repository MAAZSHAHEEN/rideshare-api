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
  `{ token, receivedAt, mode }`. `receivedAt` is the device wall-clock time the
  token arrived (D-RC-1); `mode` is the presentation-only travel mode. (Older
  values with `expiresAt` are still read; `expiresAt` is ignored.)
- Never stored: the profile (name, email, role), CNIC, phone, passwords, any
  API response. `localStorage` is never written (asserted in Vitest and
  Playwright).

### Rules (frontend/src/auth/sessionStore.ts, AuthProvider.tsx, clock.ts)

1. Written only after a successful login **and** a valid `GET /me`.
2. On load, the stored value is parsed defensively. Malformed JSON, a non-string
   or oversized token, or a token without a valid `exp` (or with `iat >= exp`) is
   deleted without any network call.
3. **Expiry is a hint; the server's 401 is the authority (D-RC-1).** The client
   never compares the token's absolute `exp` with the device clock (that broke
   sign-in for a device clock ~60 min fast and stretched sessions on a slow one).
   It uses the server-issued lifetime `exp − iat`, counted from receipt:
   - at sign-in, `remaining = (exp − iat) − time since the login response`;
   - within the page the timer runs on `performance.now()` (monotonic), so setting
     the device clock forward or back does not end or extend the session;
   - across a reload, elapsed time is `Date.now() − receivedAt` (a clock set back
     counts as 0); a consistently fast or slow clock cancels out.
   - Tokens without `iat` fall back to `exp − now` when positive, else no hint.
   - A hint that says "expired" never signs out on its own during restore: `GET /me`
     decides. A 401 then reads "Your session has expired"; any other 401 reads
     "Your session has ended". Editing `receivedAt` can at most stretch the UI hint
     to one token lifetime; the server still rejects the token at its `exp`.
4. **Restore signs out only on the server's verdict (D-RC-2).** A stored token is
   never trusted on its own: the app shows "Restoring your session…" and calls
   `GET /me`. A valid identity restores the session; 401/403 or an invalid
   profile clears it. A network error, timeout, 5xx or 429 keeps the stored
   session and shows "Can’t reach RideShare" with **Retry** and **Sign out**; no
   redirect happens and nothing from before the reload is rendered. It retries
   automatically on the browser's `online` event and on a backoff (5 s, 10 s,
   20 s, then every 30 s). An abort caused by the page unloading is ignored.
5. Any protected 401 (existing generation check), the expiry timer, focus or
   visibility checks, and logout all clear the key and the TanStack Query cache.
6. Tab scope: a new tab or window starts signed out (sessionStorage is
   per-tab), and the data disappears when the tab closes. Duplicating a tab
   copies it (browser behaviour); that copy expires with the token.
7. Return path: when a guard sends a visitor to `/login`, it passes the current
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
