from contextlib import asynccontextmanager
from fastapi import FastAPI,Depends
from database import engine, Base
import models  # noqa: F401 - imported for SQLAlchemy model registration
from routers import auth
from routers.dependencies import get_current_user
from models import User
from routers import rides
from routers import bookings

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(lifespan=lifespan)
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
    except Exception as e:
        return {"status": "Failed", "error": str(e)}

@app.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "role": current_user.role
    }
