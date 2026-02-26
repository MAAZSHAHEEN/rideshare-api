from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import Ride, RideStatus, User, UserRole
from schemas import RideCreate, RideResponse
from routers.dependencies import get_current_user

router = APIRouter(prefix="/rides", tags=["Rides"])


@router.post("/", response_model=RideResponse, status_code=201)
async def create_ride(
    ride_data: RideCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Only drivers can create rides
    if current_user.role != UserRole.driver:
        raise HTTPException(status_code=403, detail="Only drivers can create rides")

    new_ride = Ride(
        driver_id=current_user.id,
        origin=ride_data.origin,
        destination=ride_data.destination,
        departure_time=ride_data.departure_time,
        available_seats=ride_data.available_seats,
        fare_per_seat=ride_data.fare_per_seat,
    )
    db.add(new_ride)
    await db.commit()
    await db.refresh(new_ride)
    return new_ride


@router.get("/", response_model=list[RideResponse])
async def search_rides(
    origin: str,
    destination: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Ride).where(
            Ride.origin == origin,
            Ride.destination == destination,
            Ride.status == RideStatus.active,
            Ride.available_seats > 0
        )
    )
    rides = result.scalars().all()
    return rides