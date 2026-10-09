from contextlib import asynccontextmanager
from fastapi import FastAPI,Depends,HTTPException
from fastapi.middleware.cors import CORSMiddleware
from config import FRONTEND_ORIGINS
from schemas import UserResponse
from sqlalchemy import text
from database import engine
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
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(auth.router)
app.include_router(rides.router)
app.include_router(bookings.router)



@app.get("/")
def root():
    return{"message": "welcome to ride share api"}

@app.get("/test-db")
async def test_db():
    try:
        async with engine.connect() as conn:
            return {"status": "Database connected!"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable") from None

@app.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "role": current_user.role
    }
