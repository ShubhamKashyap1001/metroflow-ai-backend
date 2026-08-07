from fastapi import APIRouter
from database import supabase

router = APIRouter(
    prefix="/routes",
    tags=["Metro Routes"]
)


# Get all routes
@router.get("/")
def get_routes():

    response = supabase.table("routes").select("*").execute()

    return response.data


# Search routes
@router.get("/search")
def search_routes(
    source_station_id: str = None,
    destination_station_id: str = None
):

    query = supabase.table("routes").select("*")

    if source_station_id:
        query = query.eq(
            "source_station_id",
            source_station_id
        )

    if destination_station_id:
        query = query.eq(
            "destination_station_id",
            destination_station_id
        )

    response = query.execute()

    return response.data


# Create route
@router.post("/")
def create_route(route: dict):

    response = supabase.table("routes").insert(
        route
    ).execute()

    return {
        "message": "Route created successfully",
        "data": response.data
    }
