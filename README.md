# RideShare API

A production-ready ride-sharing backend for intercity commuters in Khyber Pakhtunkhwa, Pakistan.

Built with FastAPI, PostgreSQL, and deployed on AWS.

## Live API
https://rideshare-api.fastapicloud.dev/docs

## Tech Stack
- FastAPI
- PostgreSQL + SQLAlchemy (async)
- JWT Authentication
- Alembic (database migrations)
- Deployed on AWS EC2 with Nginx

## Features
- User registration and login with JWT
- Role-based access (passenger, driver, admin)
- Driver creates rides
- Passenger books rides
- Driver accepts or rejects bookings
- Database migrations with Alembic

## API Endpoints

### Auth
- POST /auth/register — register new user
- POST /auth/login — login and get JWT token

### Rides
- POST /rides/ — create ride (driver only)
- GET /rides/ — search rides by origin and destination

Ride creation requires a timezone-aware departure strictly in the future (for
example, an ISO 8601 timestamp with `Z` or `+05:00`). Naive or already-departed
timestamps return 422. Times are compared in UTC.

`GET /rides/` requires authentication and returns a JSON list for all roles.
Only active rides with sufficient seats and departure strictly after the
request's current UTC time are searchable. Completed, cancelled, sold-out,
and departed rides are excluded; there is no status override.

Optional query parameters:

- `origin`, `destination`: exact, case-sensitive matches; either or both may
  be omitted. Empty strings are invalid. Whitespace is not trimmed.
- `departure_from`: inclusive lower departure bound.
- `departure_before`: exclusive upper departure bound. Both time bounds require
  a timezone offset; when supplied together, the lower must precede the upper.
  Use midnight-to-midnight bounds to search a calendar day in a chosen timezone.
  A past lower bound never includes already-departed rides.
- `min_seats`: minimum available seats, default 1, range 1 through 2147483647.
- `limit`: default 20, range 1 through 100.
- `offset`: default 0, range 0 through 9223372036854775807.

Invalid query values return 422. SQL applies filters before pagination and orders
by `departure_time ASC, id ASC`. The response remains a list without a total count.
Existing callers passing both locations still work, but now receive bounded,
future-only results. Offset pages can shift as rides are added or changed.

Revision `b83d12f7a906` adds `ix_rides_status_departure_time_id` on
`(status, departure_time, id)` for the shared active/future/ordered query path.
Apply it with `alembic upgrade head`. This ordinary transactional index build
can block writes while it runs; schedule it appropriately for populated databases.

`PATCH /rides/{ride_id}/cancel` lets only the owning driver transition an active
ride to cancelled. It returns the existing ride JSON (200). Missing rides return
404, completed/already-cancelled rides return 409, and other drivers, passengers,
or admins receive 403.

The transaction locks the Ride first, then its pending/accepted bookings ordered
by booking ID. Those bookings become cancelled; rejected/cancelled history is
preserved. Each accepted booking cancelled restores one previously consumed seat,
matching passenger cancellation. Pending bookings restore none. This reverses
known debits without reconstructing original capacity; the count on a cancelled
ride is bookkeeping, not usable capacity. The ride, bookings, and seat adjustments
commit together. A cancelled ride has no pending/accepted bookings and cannot
receive new bookings. Concurrent booking actions serialize on the same Ride lock.

`PATCH /rides/{ride_id}/complete` lets only the owning driver transition an active
ride to completed and returns the existing ride JSON (200). Missing rides return
404; completed/cancelled rides return 409; other drivers, passengers, and admins
receive 403. There is no reopening or automatic time-based completion.

Completion locks and refreshes the Ride, then locks pending bookings in ID order.
Pending requests become cancelled, while accepted/rejected/cancelled history is
preserved. Seats never change during completion: accepted places remain consumed
as historical accounting. All transitions commit atomically with rollback on
failure. Completed rides have zero pending bookings. Completion and cancellation
serialize on the Ride lock, so exactly one terminal ride transition can win.

### Bookings
- POST /bookings/{ride_id} — book a ride (passenger only)
- PATCH /bookings/{booking_id}/respond — accept or reject booking (driver only)

`PATCH /bookings/{booking_id}/cancel` lets only the owning passenger cancel a
pending or accepted booking on an active ride. It returns the existing booking
JSON with status `cancelled` (200). A pending cancellation does not change seats; an accepted
cancellation restores exactly one seat in the same transaction. Rejected or
already-cancelled bookings return 409, another passenger or a non-passenger role
receives 403, and a missing booking returns 404.

Cancellation and driver decisions serialize using PostgreSQL row locks in
Ride -> Booking order. If acceptance wins first, acceptance and cancellation
may both succeed with no net seat change. Cancelled history remains stored and
does not prevent a new booking under the existing active-booking unique index.
Passenger cancellation on completed/cancelled rides returns 409. In particular,
accepted bookings on completed rides cannot be cancelled or restore seats. If
passenger cancellation wins the Ride lock before completion, its cancellation
and any valid seat restoration persist; completion may then also succeed.
No departure-time cutoff is imposed by this endpoint.

API errors use FastAPI's existing `{"detail": ...}` response. Authentication
failures return 401 with `WWW-Authenticate: Bearer`; login uses the same generic
message for unknown accounts and incorrect passwords. Permission/ownership
failures return 403 and missing resources return 404.

State conflicts return 409, including inactive rides, unavailable seats,
duplicate active bookings, invalid lifecycle transitions, and duplicate
registration details. Registration conflicts use a generic message without
identifying the conflicting field. FastAPI/Pydantic body and query validation
returns 422 with an error list; existing departure/time-window checks return
422 with a string detail. Successful response shapes are unchanged.

Only the recognized duplicate-active-booking database constraint is translated
to 409; unrelated database failures propagate normally. The `/test-db` diagnostic
returns a generic 503 on connection failure without exposing exception text.

## Booking code organization

`routers/bookings.py` declares the HTTP routes, dependencies, and response models.
`services/bookings.py` implements booking creation, driver decisions, and passenger
cancellation using the request's existing `AsyncSession`. It contains the business
rules, queries, Ride -> Booking locks, seat accounting, commits, and existing
rollback/error behavior. Services retain the existing `HTTPException` contract;
ride lifecycle operations remain in `routers/rides.py`.

## Local Setup

1. Clone the repo
   git clone https://github.com/MAAZSHAHEEN/rideshare-api.git

2. Create virtual environment
   python -m venv venv
   venv\Scripts\activate

3. Install dependencies
   pip install -r requirements.txt

4. Create .env file
   DATABASE_URL=postgresql+asyncpg://postgres:yourpassword@localhost:5432/rideshare
   SECRET_KEY=yoursecretkey
   ALGORITHM=HS256
   ACCESS_TOKEN_EXPIRE_MINUTES=60

5. Run migrations
   alembic upgrade head

6. Start server
   uvicorn main:app --reload

## CI and testing

GitHub Actions runs one Python 3.11 job on pull requests and pushes to `main`
and `rideshare-v2`. It uses a fresh PostgreSQL 15 service with a readiness health
check, installs `requirements.txt`, validates configuration, applies Alembic from
scratch, and runs the complete PostgreSQL unittest suite. Concurrency and
migration upgrade/downgrade/re-upgrade tests are included. No developer `.env`
or repository secrets are required; workflow credentials are disposable CI-only
values. The GitHub token has read-only contents permission. This workflow does
not deploy or publish images.

To run the same commands locally, first provision a disposable PostgreSQL database
whose user can create schemas and databases (`CREATEDB`). Set `DATABASE_URL` and
`TEST_DATABASE_URL` to its `postgresql+asyncpg` connection URL, set a non-placeholder
`SECRET_KEY` of at least 32 UTF-8 bytes, and set `ALGORITHM=HS256` and
`ACCESS_TOKEN_EXPIRE_MINUTES=60` in the process environment. Never use a production
database for these commands. From the repository root, with Python 3.11 activated:

```sh
python -m pip install -r requirements.txt
python -m pip check
python -m alembic upgrade head
python -B -m unittest discover -s tests -v
```

`TEST_DATABASE_URL` must be set: integration tests otherwise skip. API tests
isolate their fixtures in temporary schemas; migration tests create and remove
their own disposable databases. The initial Alembic command migrates the database
named by `DATABASE_URL`.

## Database Migrations

Alembic owns the application schema. Run `alembic upgrade head` before starting
the API against a new database. Application startup checks database connectivity
but never creates or updates tables.

The pre-production initial revision `50872040220b` now creates the current
schema. It previously contained no schema operations. A database already stamped
at that revision will not rerun it. Existing databases created by the old startup
behavior must be inspected and reconciled separately; do not blindly stamp or
reapply the initial migration over existing tables. Recreate only disposable
development databases. Do not downgrade a database containing data you need.

After changing models, always run:
   alembic revision --autogenerate -m "describe your change"
   alembic upgrade head

Review generated migrations before applying them. The PostgreSQL migration test
verifies empty-database upgrade, metadata parity, downgrade to base, and a second
upgrade. Set `TEST_DATABASE_URL` to a dedicated PostgreSQL test database and run
`python -B -m unittest discover -s tests -v`. The migration test additionally
requires `CREATEDB` permission: it creates and removes its own disposable database
on that test server, without modifying the configured database's application data.
