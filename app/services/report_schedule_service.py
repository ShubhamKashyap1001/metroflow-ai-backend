from __future__ import annotations

import asyncio
import csv
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models.metro_line import MetroLine
from app.models.report_schedule import ReportSchedule
from app.models.station import Station
from app.services import analytics_service
from app.services.report_email import send_report_email

logger = logging.getLogger(__name__)

def next_run_at(
    now: datetime,
    frequency: str,
    hour_utc: int,
    day_of_week: int | None,
) -> datetime:
    now = now.astimezone(timezone.utc)
    candidate = datetime.combine(now.date(), time(hour_utc), tzinfo=timezone.utc)
    if frequency == "daily":
        return candidate if candidate > now else candidate + timedelta(days=1)
    if day_of_week is None or not 0 <= day_of_week <= 6:
        raise HTTPException(status_code=422, detail="Choose a valid weekday for weekly reports.")
    days_ahead = (day_of_week - now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    return candidate if candidate > now else candidate + timedelta(days=7)

def _validate_filters(db: Session, station_id: int | None, line_name: str | None) -> None:
    if station_id is not None:
        station = db.get(Station, station_id)
        if not station:
            raise HTTPException(status_code=422, detail="The selected station does not exist.")
        if line_name:
            line_station = (
                db.query(MetroLine.id)
                .join(MetroLine.stations)
                .filter(MetroLine.line_name == line_name, MetroLine.is_active.is_(True))
                .filter_by(station_id=station_id)
                .first()
            )
            if not line_station:
                raise HTTPException(status_code=422, detail="The selected station is not on that metro line.")
    if line_name and not db.query(MetroLine.id).filter(
        MetroLine.line_name == line_name, MetroLine.is_active.is_(True)
    ).first():
        raise HTTPException(status_code=422, detail="The selected metro line does not exist.")

def list_user_schedules(db: Session, user_id: str) -> list[ReportSchedule]:
    return (
        db.query(ReportSchedule)
        .options(joinedload(ReportSchedule.station), joinedload(ReportSchedule.owner))
        .filter(ReportSchedule.owner_id == user_id)
        .order_by(ReportSchedule.created_at.desc(), ReportSchedule.id.desc())
        .all()
    )

def create_schedule(db: Session, user_id: str, email: str | None, payload) -> ReportSchedule:
    if not settings.ENABLE_REPORT_SCHEDULER:
        raise HTTPException(status_code=503, detail="Scheduled report delivery is disabled on this deployment.")
    if not email:
        raise HTTPException(status_code=422, detail="Add an email address to your account before scheduling reports.")
    if not settings.SMTP_HOST or not settings.SMTP_USERNAME or not settings.SMTP_FROM_EMAIL:
        raise HTTPException(status_code=503, detail="Scheduled email delivery is unavailable until SMTP is configured.")
    if db.query(ReportSchedule.id).filter(ReportSchedule.owner_id == user_id).count() >= 5:
        raise HTTPException(status_code=409, detail="Each account can have up to five report schedules.")
    _validate_filters(db, payload.station_id, payload.line_name)
    schedule = ReportSchedule(
        owner_id=user_id,
        frequency=payload.frequency,
        delivery_hour_utc=payload.delivery_hour_utc,
        day_of_week=payload.day_of_week,
        period_days=payload.period_days,
        station_id=payload.station_id,
        line_name=payload.line_name,
        state=payload.state,
        next_run_at=next_run_at(datetime.now(timezone.utc), payload.frequency, payload.delivery_hour_utc, payload.day_of_week),
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule

def update_schedule(db: Session, user_id: str, schedule_id: int, changes: dict) -> ReportSchedule:
    schedule = db.query(ReportSchedule).filter_by(id=schedule_id, owner_id=user_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Report schedule not found.")
    for field, value in changes.items():
        setattr(schedule, field, value)
    if schedule.frequency == "daily":
        schedule.day_of_week = None
    if schedule.frequency == "weekly" and schedule.day_of_week is None:
        raise HTTPException(status_code=422, detail="Choose a weekday for weekly reports.")
    if not 0 <= schedule.delivery_hour_utc <= 23 or not 1 <= schedule.period_days <= 30:
        raise HTTPException(status_code=422, detail="Report time or period is outside the allowed range.")
    _validate_filters(db, schedule.station_id, schedule.line_name)
    if "frequency" in changes or "delivery_hour_utc" in changes or "day_of_week" in changes or changes.get("is_active") is True:
        schedule.next_run_at = next_run_at(
            datetime.now(timezone.utc), schedule.frequency, schedule.delivery_hour_utc, schedule.day_of_week
        )
    db.commit()
    db.refresh(schedule)
    return schedule

def delete_schedule(db: Session, user_id: str, schedule_id: int) -> None:
    schedule = db.query(ReportSchedule).filter_by(id=schedule_id, owner_id=user_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Report schedule not found.")
    db.delete(schedule)
    db.commit()

def _csv_for_report(report: dict) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["MetroFlow Station Traffic Report"])
    writer.writerow(["Start date", report["start_date"], "End date", report["end_date"]])
    writer.writerow(["Station ID", report["station_id"] or "All stations", "Metro line", report["line_name"] or "All lines", "Region", report["state"] or "All regions"])
    writer.writerow(["Generated at", report["generated_at"].isoformat()])
    writer.writerow(["Station", "Average count", "Peak count"])
    for row in report["stations"]:
        writer.writerow([row["station_name"], row["average_count"], row["peak_count"]])
    return output.getvalue()

def run_due_report_schedules_once(db: Session, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stuck_before = now - timedelta(seconds=max(settings.REPORT_SCHEDULE_STUCK_SECONDS, 60))
    stuck = db.query(ReportSchedule).filter(
        ReportSchedule.last_run_status == "processing",
        ReportSchedule.last_run_at.isnot(None),
        ReportSchedule.last_run_at < stuck_before,
    ).all()
    for schedule in stuck:
        schedule.run_attempts += 1
        schedule.last_run_status = "failed"
        schedule.last_run_error = "A worker stopped before delivery completed; the report will be retried if attempts remain."
        if schedule.run_attempts < max(settings.REPORT_SCHEDULE_MAX_ATTEMPTS, 1):
            schedule.next_run_at = now + timedelta(seconds=max(settings.REPORT_SCHEDULE_RETRY_SECONDS, 60))
    if stuck:
        db.commit()

    schedules = (
        db.query(ReportSchedule)
        .options(joinedload(ReportSchedule.owner), joinedload(ReportSchedule.station))
        .filter(ReportSchedule.is_active.is_(True), ReportSchedule.next_run_at <= now)
        .order_by(ReportSchedule.next_run_at, ReportSchedule.id)
        .with_for_update(skip_locked=True, of=ReportSchedule)
        .limit(settings.REPORT_SCHEDULER_BATCH_SIZE)
        .all()
    )
    if not schedules:
        return {"claimed": 0, "sent": 0, "failed": 0}

    claimed = []
    for schedule in schedules:
        is_retry = (
            schedule.last_run_status == "failed"
            and schedule.run_attempts > 0
            and schedule.run_attempts < max(settings.REPORT_SCHEDULE_MAX_ATTEMPTS, 1)
        )
        claimed.append({
            "id": schedule.id,
            "email": schedule.owner.email if schedule.owner else None,
            "frequency": schedule.frequency,
            "hour": schedule.delivery_hour_utc,
            "weekday": schedule.day_of_week,
            "period_days": schedule.period_days,
            "station_id": schedule.station_id,
            "station_name": schedule.station.station_name if schedule.station else "All stations",
            "line_name": schedule.line_name,
            "state": schedule.state,
            "is_retry": is_retry,
        })
        if not is_retry:
            schedule.run_attempts = 0
            schedule.next_run_at = next_run_at(now, schedule.frequency, schedule.delivery_hour_utc, schedule.day_of_week)
        schedule.last_run_at = now
        schedule.last_run_status = "processing"
        schedule.last_run_error = None
    db.commit()

    sent = failed = 0
    end_date = now.date() - timedelta(days=1)
    for item in claimed:
        status = "failed"
        failure = None
        try:
            if not item["email"]:
                raise ValueError("The schedule owner no longer has an email address.")
            start_date = end_date - timedelta(days=item["period_days"] - 1)
            report = analytics_service.traffic_analysis_report(
                db,
                state=item["state"],
                start_date=start_date,
                end_date=end_date,
                station_id=item["station_id"],
                line_name=item["line_name"],
            )

            db.commit()
            filters = f"{start_date} to {end_date}; {item['station_name']}; {item['line_name'] or 'all lines'}"
            status = send_report_email(
                item["email"],
                _csv_for_report(report),
                "MetroFlow scheduled station traffic report",
                f"Report period: {start_date} through {end_date}\nFilters: {filters}",
            )
            if status.startswith("failed:"):
                failure = status.removeprefix("failed: ")
                status = "failed"
        except Exception as exc:
            failure = str(exc)[:500]
            logger.exception("Scheduled report %s failed", item["id"])
            db.rollback()
        record = db.query(ReportSchedule).filter_by(id=item["id"]).first()
        if record:
            record.last_run_status = status
            record.last_run_error = failure
            if status == "sent":
                record.run_attempts = 0
                if item["is_retry"]:
                    record.next_run_at = next_run_at(
                        now, item["frequency"], item["hour"], item["weekday"]
                    )
            else:
                record.run_attempts += 1
                if record.run_attempts < max(settings.REPORT_SCHEDULE_MAX_ATTEMPTS, 1):
                    record.next_run_at = now + timedelta(seconds=max(settings.REPORT_SCHEDULE_RETRY_SECONDS, 60))
                elif item["is_retry"]:
                    record.next_run_at = next_run_at(
                        now, item["frequency"], item["hour"], item["weekday"]
                    )
            db.commit()
        if status == "sent":
            sent += 1
        else:
            failed += 1
    return {"claimed": len(claimed), "sent": sent, "failed": failed}

async def run_forever(session_factory, interval_seconds: int | None = None) -> None:
    interval = max(interval_seconds or settings.REPORT_SCHEDULER_INTERVAL_SECONDS, 10)
    while True:
        def run_with_session():
            db = session_factory()
            try:
                return run_due_report_schedules_once(db)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        try:
            result = await asyncio.to_thread(run_with_session)
            if result["claimed"]:
                logger.info("[report_scheduler] %s", result)
        except Exception as exc:
            logger.error("[report_scheduler] pass failed: %s", exc, exc_info=exc)
        await asyncio.sleep(interval)
