import asyncio
import logging

from app.simulator.csv_replay_simulator import run_forever as run_crowd_forever
from app.simulator.train_simulator import run_forever as run_train_forever

logger = logging.getLogger(__name__)

_crowd_task: asyncio.Task | None = None
_train_task: asyncio.Task | None = None

def _log_task_result(name: str, task: asyncio.Task) -> None:
    """Task monitoring: run_forever() loops forever and only ever exits
    via cancellation (graceful stop) or - if something outside the
    per-tick try/except in run_forever managed to escape (e.g. the
    session_factory() call itself raising) - an unexpected crash. This
    logs the difference so a background loop dying silently doesn't go
    unnoticed until someone wonders why the dashboard stopped updating.
    """
    if task.cancelled():
        logger.info("[scheduler] %s stopped (cancelled).", name)
        return
    exc = task.exception()
    if exc is not None:
        logger.error("[scheduler] %s exited unexpectedly: %s", name, exc, exc_info=exc)
    else:
        logger.warning("[scheduler] %s exited without being cancelled - this shouldn't happen "
                        "since run_forever() loops forever.", name)

def start_simulator(session_factory, interval_seconds: int = 5) -> None:
    global _crowd_task
                                                                      
    if _crowd_task is None or _crowd_task.done():
        _crowd_task = asyncio.create_task(run_crowd_forever(session_factory, interval_seconds))
        _crowd_task.add_done_callback(lambda t: _log_task_result("crowd simulator", t))

async def stop_simulator() -> None:
    global _crowd_task
    if _crowd_task is not None:
        task, _crowd_task = _crowd_task, None
        task.cancel()
        try:
                                                                        
            await task
        except asyncio.CancelledError:
            pass

def start_train_tracker(session_factory, interval_seconds: int = 5) -> None:
    global _train_task
    if _train_task is None or _train_task.done():
        _train_task = asyncio.create_task(run_train_forever(session_factory, interval_seconds))
        _train_task.add_done_callback(lambda t: _log_task_result("train tracker", t))

async def stop_train_tracker() -> None:
    global _train_task
    if _train_task is not None:
        task, _train_task = _train_task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

def is_simulator_running() -> bool:
    return _crowd_task is not None and not _crowd_task.done()

def is_train_tracker_running() -> bool:
    return _train_task is not None and not _train_task.done()

def _task_status(task: asyncio.Task | None) -> dict:
    """One background loop's status, in enough detail to tell "never
    started" apart from "was running and crashed" apart from "was
    running and cleanly stopped" - a plain running bool collapses all
    three into the same `false`, which is exactly the distinction you
    need when a monitoring dashboard is asking "why did live updates
    stop"."""
    if task is None:
        return {"running": False, "state": "not_started"}
    if not task.done():
        return {"running": True, "state": "running"}
    if task.cancelled():
        return {"running": False, "state": "stopped"}
    if task.exception() is not None:
        return {"running": False, "state": "crashed", "error": str(task.exception())}
    return {"running": False, "state": "exited"}

def scheduler_status() -> dict:
    """Structured snapshot of both background loops (crowd simulator +
    train tracker) - consumed by the /health endpoint so external
    liveness/monitoring checks can see at a glance whether either loop
    has silently died, and by /admin/simulator for the same detail in
    the admin UI."""
    return {
        "crowd_simulator": _task_status(_crowd_task),
        "train_tracker": _task_status(_train_task),
    }
