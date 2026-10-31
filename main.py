from contextlib import asynccontextmanager
from fastapi import FastAPI,Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from validation_errors import request_validation_error_handler
from config import FRONTEND_ORIGINS, AUTH_RATE_LIMIT_SETTINGS, MAX_REQUEST_BODY_BYTES
from auth_rate_limit import AuthRateLimiter, AuthRateLimitMiddleware
from http_safety import RequestBodyLimitMiddleware, RequestIdMiddleware, SafeErrorMiddleware
from schemas import UserResponse
from sqlalchemy import text
from database import engine
from frontend_serving import register_frontend
import health
import models  # noqa: F401 - imported for SQLAlchemy model registration
from routers import auth
from routers.dependencies import get_current_user
from models import User
from routers import rides
from routers import bookings

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alembic owns schema changes; startup only verifies database connectivity.
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    try:
        yield
    finally:
        await app.state.auth_rate_limiter.close()

app = FastAPI(lifespan=lifespan)
app.state.auth_rate_limiter = AuthRateLimiter(AUTH_RATE_LIMIT_SETTINGS)
app.add_exception_handler(RequestValidationError, request_validation_error_handler)
# Middleware added first runs innermost. The inner SafeErrorMiddleware turns route
# exceptions into a generic 500 that CORS still decorates; the outer one covers
# the middleware layers. Neither re-raises, so Uvicorn never logs the traceback.
app.add_middleware(SafeErrorMiddleware)
app.add_middleware(AuthRateLimitMiddleware)
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Retry-After", "X-Request-ID"],
)
app.add_middleware(SafeErrorMiddleware)
app.add_middleware(RequestIdMiddleware)
app.include_router(auth.router)
app.include_router(rides.router)
app.include_router(bookings.router)
# Liveness/readiness probes; the old /test-db diagnostic was retired (AST-010).
app.include_router(health.router)

@app.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "role": current_user.role
    }


register_frontend(app)
