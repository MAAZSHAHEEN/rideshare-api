# RideShare frontend foundation

React + TypeScript + Vite, React Router, TanStack Query and a small typed fetch
client. All files for this task live in `frontend/`; the backend is unchanged.

## Local development

Use Node 22.12+ (Node 22 LTS recommended) and npm:

```sh
cd frontend
npm install
```

Copy `.env.example` to `.env.local` if the backend is not at
`http://localhost:8000`. `VITE_API_BASE_URL` is public build-time configuration,
not a place for credentials. Restart Vite after changing it.

Run the existing backend from the repository root with its usual database and
JWT environment settings. Add this process environment variable (PowerShell):

```powershell
$env:FRONTEND_ORIGINS = 'http://127.0.0.1:5173,http://localhost:5173'
.\venv\Scripts\python.exe -m uvicorn main:app --reload
```

The database must already be migrated. This frontend does not run migrations.
Then, in `frontend/`:

```sh
npm run dev
```

Open **http://127.0.0.1:5173**. On Windows systems blocking PowerShell npm scripts,
use `npm.cmd` instead of `npm`. No backend source or local backend `.env` edits
are needed. Set the exact production frontend origin in the existing backend
configuration when deploying separately.

## Validation

```sh
npm run typecheck
npm run build
npm test
npx playwright install chromium
npm run test:e2e
```

To use an already installed Chrome on Windows instead of downloading Chromium:

```powershell
$env:PLAYWRIGHT_CHANNEL = 'chrome'
npm run test:e2e
```

Browser tests start Vite at port 5183 and intercept API calls using contract
fixtures. They do not create real accounts and are not a replacement for the
backend PostgreSQL suite. Screenshots are saved under ignored `test-results/`.
For manual integration, register a real test account against your local backend.

## Scope and session policy

- Landing, responsive navigation/footer, login and registration, separate
  passenger/driver layouts, read-only paginated history, and a read-only account.
- Registration sends name, email, password, CNIC, phone number and selected role;
  it redirects to login. No token is assumed from registration.
- Passwords are preserved exactly. New passwords require 12 Unicode characters,
  at most 72 UTF-8 bytes, and cannot consist only of backend-defined whitespace.
  Login allows existing shorter passwords.
- Login exchanges credentials, then verifies identity using `GET /me`. JWT
  decoding is used only for an expiry timer, never to grant roles.
- Tokens exist only in memory. Reloads and new tabs require login. Logout clears
  credentials, aborts sign-in, cancels queries and clears the query cache. An old
  request cannot invalidate a newer session. Logout does not revoke server JWTs.
- Authenticated 401s and expiry end the session. Protected route checks are UI
  controls; backend authorization remains authoritative.
- Mutations are not retried automatically. Registration network/server failures
  explain that persistence may have occurred before the response failed.
- History uses `/rides/me` or `/bookings/me`, limit 20 and offset pagination.
  Counts describe the displayed page, not lifetime totals. All history is shown;
  active rides are not automatically called upcoming. Times use the browser's
  timezone. Currency is intentionally not assumed.
- No ride creation/booking decisions/cancellation UI in this foundation task.
  No fake trips, maps, ratings, payments, social login or admin console.

Production output is `dist/`. Static hosting must fall back to `index.html` for
client routes. Serve over HTTPS. Google Fonts supplies optional typography;
system fonts remain usable when offline. Travel artwork is a local SVG with no
external image dependency. Keep API/session values out of analytics and logs.

## Organization

`src/api/` holds backend types, fetch and error handling; `src/auth/` owns memory
sessions and password validation; `src/pages/` holds screens; `src/components/`
contains shared navigation/icons. Server state uses TanStack Query; form state
stays local. Tests cover API contracts, session races, forms and browser layouts.
