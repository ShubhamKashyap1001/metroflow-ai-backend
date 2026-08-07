from fastapi import APIRouter
from database import supabase

router = APIRouter(
    prefix="/stations",
    tags=["Stations"]
)


# Get all stations
@router.get("/")
def get_stations():
    response = supabase.table("stations").select("*").execute()
    return response.data


# Get station by name
@router.get("/{station_name}")
def get_station(station_name: str):

    response = supabase.table("stations").select("*").eq(
        "station_name",
        station_name
    ).execute()

    if not response.data:
        return {
            "message": "Station not found"
        }

    return response.data[0]