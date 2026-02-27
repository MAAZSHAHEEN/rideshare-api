# RideShare API

A production-ready ride-sharing backend for intercity commuters in Khyber Pakhtunkhwa, Pakistan.

Built with FastAPI, PostgreSQL, and deployed on AWS.

## Live API
http://3.147.63.125/docs

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

After changing models, always run:
   alembic revision --autogenerate -m "describe your change"
   alembic upgrade head