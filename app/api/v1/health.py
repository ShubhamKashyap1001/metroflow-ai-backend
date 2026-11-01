import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text

from app.core import cache
from app.core.config import settings
from app.database.database import engine
from app.simulator.scheduler import scheduler_status
from app.websocket.manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/health",
    tags=["Health"]
)

@router.get("/")
def health_check():
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:

        logger.error("[health] database check failed: %s", exc, exc_info=exc)
        db_status = "error"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "app_name": settings.APP_NAME,
        "app_version": settings.APP_VERSION,
        "database": db_status,
                                                                 
        "scheduler": scheduler_status(),
                                                                 
        "redis": cache.redis_status(),
        "websocket_connections": len(manager.active_connections),
        "timestamp": datetime.now(timezone.utc),
    }

