from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.reporting import ReportScheduleCreate
from app.services.report_schedule_service import next_run_at

def test_daily_report_schedule_advances_to_next_day_after_delivery_hour():
    now = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
    assert next_run_at(now, "daily", 8, None) == datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)

def test_weekly_report_schedule_advances_to_next_matching_weekday():
    now = datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc)  
    assert next_run_at(now, "weekly", 8, 0) == datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)

def test_weekly_report_requires_a_delivery_day():
    with pytest.raises(ValidationError):
        ReportScheduleCreate(frequency="weekly", day_of_week=None)

def test_report_schedule_period_is_capped_at_30_days():
    with pytest.raises(ValidationError):
        ReportScheduleCreate(frequency="daily", period_days=31)
