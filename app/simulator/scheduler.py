import functools

from app.simulator.csv_replay_simulator import run_forever as run_crowd_forever
from app.simulator.leader_election import LeaderElection
from app.simulator.control import get_runtime_control, update_runtime_control
from app.simulator.notification_bin_retention import (
    run_forever as run_notification_bin_retention_forever,
)
from app.services.report_schedule_service import run_forever as run_report_schedule_forever
from app.simulator.retention import run_forever as run_retention_forever
from app.simulator.train_simulator import run_forever as run_train_forever

_crowd_election: LeaderElection | None = None
_train_election: LeaderElection | None = None
_retention_election: LeaderElection | None = None
_notification_bin_retention_election: LeaderElection | None = None
_report_schedule_election: LeaderElection | None = None

def start_simulator(session_factory, interval_seconds: int = 60) -> None:
    global _crowd_election
    if _crowd_election is None:
        _crowd_election = LeaderElection(
            "crowd_simulator",
            functools.partial(
                run_crowd_forever,
                session_factory,
                interval_seconds,
                control_provider=get_runtime_control,
            ),
        )
    _crowd_election.start()

def resume_simulator(session_factory, interval_seconds: int = 60) -> None:
    update_runtime_control(crowd_enabled=True)
    start_simulator(session_factory, interval_seconds)

def pause_simulator() -> None:
    update_runtime_control(crowd_enabled=False)

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
            functools.partial(
                run_train_forever,
                session_factory,
                interval_seconds,
                control_provider=get_runtime_control,
            ),
        )
    _train_election.start()

def resume_train_tracker(session_factory, interval_seconds: int = 60) -> None:
    update_runtime_control(train_enabled=True)
    start_train_tracker(session_factory, interval_seconds)

def pause_train_tracker() -> None:
    update_runtime_control(train_enabled=False)

async def stop_train_tracker() -> None:
    global _train_election
    if _train_election is not None:
        election, _train_election = _train_election, None
        await election.stop()

def is_simulator_running() -> bool:
    return (
        get_runtime_control()["crowd_enabled"]
        and _crowd_election is not None
        and _crowd_election.is_active()
    )

def is_train_tracker_running() -> bool:
    return (
        get_runtime_control()["train_enabled"]
        and _train_election is not None
        and _train_election.is_active()
    )

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

def start_report_scheduler(session_factory, interval_seconds: int | None = None) -> None:
    global _report_schedule_election
    if _report_schedule_election is None:
        _report_schedule_election = LeaderElection(
            "report_scheduler",
            functools.partial(run_report_schedule_forever, session_factory, interval_seconds),
        )
    _report_schedule_election.start()

async def stop_report_scheduler() -> None:
    global _report_schedule_election
    if _report_schedule_election is not None:
        election, _report_schedule_election = _report_schedule_election, None
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
    return [
        _election_metrics_snapshot("crowd_simulator", _crowd_election),
        _election_metrics_snapshot("train_tracker", _train_election),
        _election_metrics_snapshot("crowd_retention_job", _retention_election),
        _election_metrics_snapshot(
            "notification_bin_retention_job", _notification_bin_retention_election
        ),
        _election_metrics_snapshot("report_scheduler", _report_schedule_election),
    ]

def scheduler_status() -> dict:
    control = get_runtime_control()
    crowd_status = _election_status(_crowd_election)
    train_status = _election_status(_train_election)
    if not control["crowd_enabled"]:
        crowd_status = {**crowd_status, "running": False, "state": "paused"}
    if not control["train_enabled"]:
        train_status = {**train_status, "running": False, "state": "paused"}
    crowd_status.update(
        enabled=control["crowd_enabled"],
        interval_seconds=control["crowd_interval_seconds"],
    )
    train_status.update(
        enabled=control["train_enabled"],
        interval_seconds=control["train_interval_seconds"],
    )
    return {
        "crowd_simulator": crowd_status,
        "train_tracker": train_status,
        "crowd_retention_job": _election_status(_retention_election),
        "notification_bin_retention_job": _election_status(_notification_bin_retention_election),
        "report_scheduler": _election_status(_report_schedule_election),
    }
