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

### Bookings
- POST /bookings/{ride_id} — book a ride (passenger only)
- PATCH /bookings/{booking_id}/respond — accept or reject booking (driver only)

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
