from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# hide_parameters: bound values (CNIC, phone, password hashes) never appear in
# SQLAlchemy exception text or SQL echo/debug logs, e.g. an unexpected
# IntegrityError traceback logged by the server.
engine = create_async_engine(DATABASE_URL, hide_parameters=True)

SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

async def get_db():
    async with SessionLocal() as session:
        yield session