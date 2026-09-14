import functools

from app.simulator.csv_replay_simulator import run_forever as run_crowd_forever
from app.simulator.leader_election import LeaderElection
from app.simulator.notification_bin_retention import (
    run_forever as run_notification_bin_retention_forever,
)
from app.simulator.retention import run_forever as run_retention_forever
from app.simulator.train_simulator import run_forever as run_train_forever

_crowd_election: LeaderElection | None = None
_train_election: LeaderElection | None = None
_retention_election: LeaderElection | None = None
_notification_bin_retention_election: LeaderElection | None = None


def start_simulator(session_factory, interval_seconds: int = 60) -> None:
    global _crowd_election
    if _crowd_election is None:
        _crowd_election = LeaderElection(
            "crowd_simulator",
            functools.partial(run_crowd_forever, session_factory, interval_seconds),
        )
    _crowd_election.start()

async def stop_simulator() -> None:
    global _crowd_election
    if _crowd_election is not None:
        election, _crowd_election = _crowd_election, None
        await election.stop()

def start_train_tracker(session_factory, interval_seconds: int = 60) -> None:
    global _train_election
    if _train_election is None:
        _train_election = LeaderElection(
            "train_tracker",
            functools.partial(run_train_forever, session_factory, interval_seconds),
        )
    _train_election.start()

async def stop_train_tracker() -> None:
    global _train_election
    if _train_election is not None:
        election, _train_election = _train_election, None
        await election.stop()

def is_simulator_running() -> bool:
    return _crowd_election is not None and _crowd_election.is_active()

def is_train_tracker_running() -> bool:
    return _train_election is not None and _train_election.is_active()

def start_retention_job(session_factory, interval_seconds: int | None = None) -> None:
    global _retention_election
    if _retention_election is None:
        _retention_election = LeaderElection(
            "crowd_retention_job",
            functools.partial(run_retention_forever, session_factory, interval_seconds),
        )
    _retention_election.start()

async def stop_retention_job() -> None:
    global _retention_election
    if _retention_election is not None:
        election, _retention_election = _retention_election, None
        await election.stop()

def is_retention_job_running() -> bool:
    return _retention_election is not None and _retention_election.is_active()

def start_notification_bin_retention_job(session_factory, interval_seconds: int | None = None) -> None:
    global _notification_bin_retention_election
    if _notification_bin_retention_election is None:
        _notification_bin_retention_election = LeaderElection(
            "notification_bin_retention_job",
            functools.partial(run_notification_bin_retention_forever, session_factory, interval_seconds),
        )
    _notification_bin_retention_election.start()

async def stop_notification_bin_retention_job() -> None:
    global _notification_bin_retention_election
    if _notification_bin_retention_election is not None:
        election, _notification_bin_retention_election = _notification_bin_retention_election, None
        await election.stop()

def is_notification_bin_retention_job_running() -> bool:
    return (
        _notification_bin_retention_election is not None
        and _notification_bin_retention_election.is_active()
    )


def _election_status(election: LeaderElection | None) -> dict:
    if election is None:
        return {"running": False, "state": "not_started"}
    return election.status()

def _election_metrics_snapshot(name: str, election: LeaderElection | None) -> dict:
    if election is None:
        return {
            "name": name,
            "state": "not_started",
            "heartbeat_ticks_total": 0,
            "leadership_acquired_total": 0,
            "leadership_lost_total": 0,
            "worker_crashes_total": 0,
            "last_heartbeat_ts": None,
        }
    return election.get_metrics_snapshot()

def scheduler_metrics_snapshot() -> list[dict]:
    """Per-loop leader/heartbeat snapshots for app/core/metrics.py's
    simulator collector - the metrics-shaped counterpart to
    scheduler_status() above. Each entry always carries the loop's own
    `name` (rather than requiring the caller to already know the four
    fixed loop names), so a not-yet-started loop still yields a zeroed
    row instead of being silently omitted."""
    return [
        _election_metrics_snapshot("crowd_simulator", _crowd_election),
        _election_metrics_snapshot("train_tracker", _train_election),
        _election_metrics_snapshot("crowd_retention_job", _retention_election),
        _election_metrics_snapshot(
            "notification_bin_retention_job", _notification_bin_retention_election
        ),
    ]

def scheduler_status() -> dict:
    """Structured snapshot of all three background loops - consumed by
    the /health endpoint so external liveness/monitoring checks can see
    at a glance whether either loop has silently died, and by
    /admin/simulator for the same detail in the admin UI. `state` is
    now one of "not_started" | "leader" | "standby" | "crashed" - a
    process reporting "standby" is healthy and expected in a
    multi-worker deployment: it means another process is holding
    leadership for that loop, not that anything is broken."""
    return {
        "crowd_simulator": _election_status(_crowd_election),
        "train_tracker": _election_status(_train_election),
        "crowd_retention_job": _election_status(_retention_election),
        "notification_bin_retention_job": _election_status(_notification_bin_retention_election),
    }
