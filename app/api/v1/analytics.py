from fastapi import APIRouter
from database import supabase

router = APIRouter(
    prefix="/analytics",
    tags=["Analytics"]
)


# Get all analytics
@router.get("/")
def get_analytics():

    response = supabase.table("analytics").select("*").execute()

    return response.data


# Create analytics
@router.post("/")
def create_analytics(analytics: dict):

    response = supabase.table("analytics").insert(
        analytics
    ).execute()

    return {
        "message": "Analytics created successfully",
        "data": response.data
    }
