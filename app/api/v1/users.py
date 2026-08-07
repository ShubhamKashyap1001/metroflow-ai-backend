from fastapi import APIRouter
from database import supabase


router = APIRouter(
    prefix="/users",
    tags=["Users"]
)


# Get all users
@router.get("/")
def get_users():

    response = supabase.table("users").select("*").execute()

    return response.data



# Create user
@router.post("/")
def create_user(user: dict):

    response = supabase.table("users").insert(
        user
    ).execute()

    return {
        "message": "User created successfully",
        "data": response.data
    }
