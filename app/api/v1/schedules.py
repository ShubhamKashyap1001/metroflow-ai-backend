from fastapi import APIRouter
from database import supabase


router = APIRouter(
    prefix="/schedules",
    tags=["Schedules"]
)


# Get all schedules
@router.get("/")
def get_schedules():

    response = supabase.table("schedules").select("*").execute()

    return response.data



# Create schedule
@router.post("/")
def create_schedule(schedule: dict):

    response = supabase.table("schedules").insert(
        schedule
    ).execute()

    return {
        "message": "Schedule created successfully",
        "data": response.data
    }
