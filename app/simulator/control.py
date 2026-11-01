from __future__ import annotations

import asyncio
import threading

from app.core import cache
from app.core.config import settings

CONTROL_KEY = "simulator:runtime_control"
_lock = threading.Lock()
_local_control: dict | None = None

def default_runtime_control() -> dict:
    return {
        "crowd_enabled": settings.ENABLE_SIMULATOR,
        "train_enabled": settings.ENABLE_TRAIN_TRACKING,
        "crowd_interval_seconds": settings.SIMULATOR_INTERVAL_SECONDS,
        "train_interval_seconds": settings.TRAIN_TRACK_INTERVAL_SECONDS,
    }

def get_runtime_control() -> dict:
    remote = cache.get_json(CONTROL_KEY)
    if isinstance(remote, dict):
        return {**default_runtime_control(), **remote}
    with _lock:
        return {**default_runtime_control(), **(_local_control or {})}

def update_runtime_control(**changes) -> dict:
    global _local_control
    updated = {**get_runtime_control(), **changes}
    with _lock:
        _local_control = updated
    cache.set_json_persistent(CONTROL_KEY, updated)
    return updated

def control_coordination() -> str:
    return "redis" if cache.redis_status().get("connected") else "process-local"

async def wait_for_controlled_tick(kind: str, interval_seconds: int, control_provider) -> None:
    remaining = interval_seconds
    while remaining > 0:
        wait_seconds = min(2, remaining)
        await asyncio.sleep(wait_seconds)
        remaining -= wait_seconds
        control = control_provider()
        if not control.get(f"{kind}_enabled", False):
            return
        if int(control.get(f"{kind}_interval_seconds", interval_seconds)) != interval_seconds:
            return
