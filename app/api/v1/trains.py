from fastapi import APIRouter
from database import supabase


router = APIRouter(
    prefix="/trains",
    tags=["Trains"]
)


# Get all trains
@router.get("/")
def get_trains():

    response = supabase.table("trains").select("*").execute()

    return response.data



# Create train
@router.post("/")
def create_train(train: dict):

    response = supabase.table("trains").insert(
        train
    ).execute()

    return {
        "message": "Train created successfully",
        "data": response.data
    }
