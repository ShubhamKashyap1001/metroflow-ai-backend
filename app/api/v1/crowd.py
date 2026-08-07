from fastapi import APIRouter
from database import supabase


router = APIRouter(
    prefix="/crowd",
    tags=["Crowd Management"]
)


# Get all crowd snapshots
@router.get("/")
def get_crowd_data():

    response = supabase.table("crowd_snapshots").select("*").execute()

    return response.data



# Add crowd data
@router.post("/")
def create_crowd_data(crowd: dict):

    response = supabase.table("crowd_snapshots").insert(
        crowd
    ).execute()

    return {
        "message": "Crowd data added successfully",
        "data": response.data
    }



# Get crowd by station id
@router.get("/{station_id}")
def get_station_crowd(station_id: str):

    response = supabase.table("crowd_snapshots").select("*").eq(
        "station_id",
        station_id
    ).execute()


    if not response.data:
        return {
            "message": "Station crowd data not found"
        }


    return response.data[0]
