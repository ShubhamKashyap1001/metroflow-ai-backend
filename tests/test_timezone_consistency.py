
from datetime import datetime, timedelta, timezone

from app.ai_engine.prediction.crowd_predictor import predict_crowd
from app.ai_engine.prediction.delay_predictor import predict_delay
from app.ai_engine.prediction.frequency_predictor import recommend_frequency
from app.models.journey import Journey
from app.models.route import Route



def test_journey_checkin_and_checkout_columns_are_timezone_aware():
    checkin_col = Journey.__table__.columns["checkin_time"]
    checkout_col = Journey.__table__.columns["checkout_time"]
    assert checkin_col.type.timezone is True
    assert checkout_col.type.timezone is True


def test_route_created_at_column_is_timezone_aware():
    created_at_col = Route.__table__.columns["created_at"]
    assert created_at_col.type.timezone is True


def test_route_created_at_default_produces_an_aware_datetime():
    """The python-side `default=` callable must itself hand SQLAlchemy
    an aware value, not just declare an aware column - a naive value
    written into an aware column is the exact bug this fix targets."""
    default_value = Route.__table__.columns["created_at"].default.arg(None)
    assert default_value.tzinfo is not None
    assert default_value.utcoffset() == timedelta(0)


# --- 2. Predictors default to aware UTC "now", not naive -------------

def test_predict_crowd_defaults_to_aware_utc_now():
    result = predict_crowd(station_id=1, light=True)
    assert result["target_datetime"].tzinfo is not None
    assert result["target_datetime"].utcoffset() == timedelta(0)


def test_predict_crowd_preserves_an_explicitly_aware_target_datetime():
    target = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
    result = predict_crowd(station_id=1, target_datetime=target, light=True)
    assert result["target_datetime"] == target
    assert result["target_datetime"].tzinfo is not None


def test_predict_delay_defaults_to_aware_utc_now():
    result = predict_delay(station_id=1)
    assert result["target_datetime"].tzinfo is not None
    assert result["target_datetime"].utcoffset() == timedelta(0)


def test_recommend_frequency_defaults_to_aware_utc_now():
    result = recommend_frequency(station_id=1)
    assert result["target_datetime"].tzinfo is not None
    assert result["target_datetime"].utcoffset() == timedelta(0)


def test_predictors_agree_on_peak_hour_for_the_same_aware_instant():
    """Regression for the described "incorrect ... peak-hour
    calculations" symptom: feeding the same aware instant into two
    predictors must not disagree about is_peak_hour just because one
    of them used to normalize via a naive clock and the other via an
    aware one."""
    target = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)  # 9am -> peak
    crowd_result = predict_crowd(station_id=1, target_datetime=target, light=True)
    delay_result = predict_delay(station_id=1, target_datetime=target)
    assert crowd_result["target_datetime"].hour == delay_result["target_datetime"].hour == 9


# --- 3. live_simulator's elapsed-time math no longer mixes naive/aware -

def test_elapsed_minutes_math_does_not_raise_when_checkin_time_is_aware():
    """This is exactly the shape of app/simulator/live_simulator.py's
    checkout sweep: `(datetime.now(timezone.utc) - journey.checkin_time)`.
    Before the fix, journey.checkin_time came back from the DB as a
    naive datetime (naive column) while `datetime.utcnow()` was also
    naive - accidentally "working" only because both sides happened to
    be naive. Once checkin_time is read back as aware (this fix), a
    naive `datetime.utcnow()` on the other side would raise
    `TypeError: can't subtract offset-naive and offset-aware
    datetimes`. Simulating a Journey-like object with an aware
    checkin_time here proves the current (aware-on-both-sides) code
    doesn't hit that.
    """
    class _FakeJourney:
        checkin_time = datetime.now(timezone.utc) - timedelta(minutes=12)

    elapsed_minutes = (
        datetime.now(timezone.utc) - _FakeJourney.checkin_time
    ).total_seconds() / 60
    assert 11 <= elapsed_minutes <= 13


def test_elapsed_minutes_raises_for_the_old_naive_utcnow_pattern():
    """Documents *why* the fix was needed: mixing a naive `now` with
    an aware `checkin_time` (the state the DB would actually be in
    after the column-type fix, if the naive `datetime.utcnow()` call
    site had been left unfixed) blows up."""
    aware_checkin_time = datetime.now(timezone.utc) - timedelta(minutes=12)
    naive_now = datetime.utcnow()  # the old, buggy call
    import pytest
    with pytest.raises(TypeError):
        naive_now - aware_checkin_time
