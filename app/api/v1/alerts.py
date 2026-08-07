from fastapi import APIRouter, Depends
from database import supabase
from dependencies import get_current_user

router = APIRouter(
    prefix="/alerts",
    tags=["Alerts"]
)


@router.get("/")
def get_alerts():
    response = (
        supabase
        .table("service_alerts")
        .select("*")
        .execute()
    )
    return response.data


@router.post("/")
def create_alert(
    alert: dict,
    current_user: dict = Depends(get_current_user)
):
    response = (
        supabase
        .table("service_alerts")
        .insert(alert)
        .execute()
    )

    return response.data
