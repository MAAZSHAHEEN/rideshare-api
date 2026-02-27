# RideShare API

A production-ready ride-sharing backend for intercity commuters in Khyber Pakhtunkhwa, Pakistan.

Built with FastAPI, PostgreSQL, and deployed on AWS.

## Tech Stack
- FastAPI
- PostgreSQL + SQLAlchemy (async)
- JWT Authentication
- Deployed on AWS EC2 with Nginx

## Features
- User registration and login with JWT
- Role-based access (passenger, driver, admin)
- Driver creates rides
- Passenger books rides
- Driver accepts or rejects bookings

## API Endpoints

### Auth
- POST /auth/register
- POST /auth/login

### Rides
- POST /rides/ — create ride (driver only)
- GET /rides/ — search rides

### Bookings
- POST /bookings/{ride_id} — book a ride (passenger only)
- PATCH /bookings/{booking_id}/respond — accept or reject (driver only)

## Live API
http://3.147.63.125/docs

## Local Setup

1. Clone the repo
2. Create a virtual environment
3. Install dependencies: pip install -r requirements.txt
4. Create .env file with your database URL and secret key
5. Run: uvicorn main:app --reload
