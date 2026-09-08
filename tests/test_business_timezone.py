from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.utils.timezone import business_today, to_business_time



def test_to_business_time_converts_utc_to_ist_offset():
    """Asia/Kolkata is a fixed UTC+5:30 offset (no DST) - a UTC instant
    must come back exactly 5.5 hours later in local wall-clock terms."""
    dt = datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc)
    local = to_business_time(dt)
    assert local.utcoffset() == timedelta(hours=5, minutes=30)
    assert (local.hour, local.minute) == (14, 30)
    # The conversion doesn't change the underlying instant.
    assert local.astimezone(timezone.utc) == dt


def test_to_business_time_can_roll_the_calendar_date_forward():
    """This is the exact shape of the day_type bug: a late-UTC-Friday
    instant is already business-local Saturday."""
    friday_utc = datetime(2026, 6, 5, 19, 0, tzinfo=timezone.utc)  # Friday
    assert friday_utc.weekday() == 4  # Friday
    local = to_business_time(friday_utc)
    assert local.weekday() == 5  # Saturday in Asia/Kolkata
    assert local.date() == date(2026, 6, 6)


def test_to_business_time_treats_naive_input_as_utc():
    """Defensive: a naive datetime is treated as UTC rather than raising
    or silently being interpreted as already-local."""
    naive = datetime(2026, 6, 3, 9, 0)
    local = to_business_time(naive)
    assert local.tzinfo is not None
    assert (local.hour, local.minute) == (14, 30)


def test_business_today_is_a_plain_date_in_the_business_timezone():
    assert isinstance(business_today(), date)


def test_current_day_type_is_weekend_when_business_local_already_rolled_over(monkeypatch):
    """UTC Friday night that is already Saturday morning in Asia/Kolkata
    must resolve as WEEKEND - the exact case a raw-UTC weekday check
    gets wrong."""
    from app.enums.day_type import DayType
    from app.services import schedule_service

    saturday_ist = datetime(2026, 6, 6, 0, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert saturday_ist.weekday() == 5
    monkeypatch.setattr(schedule_service, "business_now", lambda: saturday_ist)

    assert schedule_service._current_day_type() == DayType.WEEKEND


def test_current_day_type_is_weekday_when_business_local_has_not_rolled_over_yet(monkeypatch):
    """UTC still shows Sunday night, but it's already Monday morning in
    Asia/Kolkata - must resolve as WEEKDAY."""
    from app.enums.day_type import DayType
    from app.services import schedule_service

    monday_ist = datetime(2026, 6, 8, 0, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert monday_ist.weekday() == 0
    monkeypatch.setattr(schedule_service, "business_now", lambda: monday_ist)

    assert schedule_service._current_day_type() == DayType.WEEKDAY


def test_recommend_frequency_flags_peak_hour_from_business_local_time_not_raw_utc():
    """09:00 UTC is 14:30 in Asia/Kolkata - off-peak locally, even though
    9 falls inside the 8-11 peak WINDOW as a raw hour number. The old
    (buggy) code read `dt.hour` directly and would have flagged this as
    peak."""
    from app.ai_engine.prediction.frequency_predictor import recommend_frequency

    target = datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc)
    result = recommend_frequency(station_id=1, target_datetime=target)
    assert result["is_peak_hour"] is False


def test_recommend_frequency_flags_peak_hour_correctly_for_the_matching_utc_instant():
    """03:30 UTC is 09:00 in Asia/Kolkata - genuinely peak locally, even
    though 3 is nowhere near the 8-11/17-20 windows as a raw UTC hour."""
    from app.ai_engine.prediction.frequency_predictor import recommend_frequency

    target = datetime(2026, 6, 3, 3, 30, tzinfo=timezone.utc)
    result = recommend_frequency(station_id=1, target_datetime=target)
    assert result["is_peak_hour"] is True



def test_real_train_age_days_uses_business_today(monkeypatch):
    from app.ai_engine.prediction import delay_predictor

    fixed_today = date(2026, 6, 6)
    monkeypatch.setattr(delay_predictor, "business_today", lambda: fixed_today)

    fake_train = MagicMock()
    fake_train.commissioned_date = date(2020, 6, 6)
    fake_db = MagicMock()
    fake_db.get.return_value = fake_train

    age_days = delay_predictor._real_train_age_days(fake_db, train_id=42)
    assert age_days == (fixed_today - fake_train.commissioned_date).days


def test_compute_fleet_stats_uses_business_today_for_average_age(monkeypatch):
    from app.ai_engine.prediction import delay_predictor

    fixed_today = date(2026, 6, 6)
    monkeypatch.setattr(delay_predictor, "business_today", lambda: fixed_today)

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.all.return_value = [
        (1200, date(2020, 6, 6)),
        (1200, date(2022, 6, 6)),
    ]

    avg_capacity, avg_age_days = delay_predictor._compute_fleet_stats(fake_db)
    expected_ages = [
        (fixed_today - date(2020, 6, 6)).days,
        (fixed_today - date(2022, 6, 6)).days,
    ]
    assert avg_age_days == sum(expected_ages) / len(expected_ages)
    assert avg_capacity == 1200.0
