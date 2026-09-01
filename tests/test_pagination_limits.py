"""Phase 9 - response-size / server-side-cost limits for the alert,
notification, prediction-history and demand-forecast list endpoints.

These are pure unit tests against the service layer, isolated from the
DB/Redis layers via MagicMock/patch - same style and same reason as
tests/test_prediction_service.py: this sandbox has no network access
to install fastapi/sqlalchemy/redis, so a real TestClient + Postgres
run isn't possible here. They're written to run unmodified in the
project's normal `pip install -r requirements.txt && pytest` setup.

Each test simulates a "large dataset" by having the mocked query
return however many rows the *clamped* limit asks for, then asserts
the clamp was actually applied to the query (offset/limit call args),
not just that the function returned something. This is the part that
actually protects the server: a caller asking for a huge page size
must never reach `.all()` with no bound at all.
"""
from unittest.mock import MagicMock, patch

from app.services import alert_service
from app.services import analytics_service
from app.services import notification_service
from app.services import prediction_service


# --- alert_service.list_alerts ---------------------------------------------

def _query_chain(db: MagicMock):
    """Return the single mock object representing
    db.query(...).order_by(...) - the point at which .offset()/.limit()
    are chained on in every fixed service function under test here."""
    return db.query.return_value.order_by.return_value


def test_list_alerts_clamps_oversized_limit_and_paginates():
    """A caller asking for an absurd page size (simulating someone
    trying to pull the whole alerts table in one shot) must be capped
    at MAX_ALERTS_LIMIT, and the cap must actually reach the DB query
    (.limit()), not just get silently ignored."""
    db = MagicMock()
    chain = _query_chain(db)
    # Simulate the "large dataset": server only ever returns up to the
    # clamped page size, never the full requested amount.
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(alert_service.MAX_ALERTS_LIMIT)
    ]

    result = alert_service.list_alerts(db, limit=10_000_000, offset=0)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(alert_service.MAX_ALERTS_LIMIT)
    assert len(result) == alert_service.MAX_ALERTS_LIMIT


def test_list_alerts_default_limit_used_when_not_specified():
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    alert_service.list_alerts(db)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(alert_service.DEFAULT_ALERTS_LIMIT)


def test_list_alerts_rejects_negative_offset():
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    alert_service.list_alerts(db, offset=-50)

    chain.offset.assert_called_once_with(0)


def test_list_alerts_zero_or_negative_limit_falls_back_to_at_least_one():
    """limit=0 (or negative) must not turn into an unbounded/empty-cap
    query - it's floored at 1, matching the admin /logs clamp pattern
    (`min(max(limit, 1), cap)`)."""
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    alert_service.list_alerts(db, limit=0)
    chain.offset.return_value.limit.assert_called_with(1)

    alert_service.list_alerts(db, limit=-5)
    chain.offset.return_value.limit.assert_called_with(1)


# --- alert_service.list_alert_notifications --------------------------------

def test_list_alert_notifications_clamps_oversized_limit():
    """Per-recipient delivery log for one alert - simulate an alert
    that fanned out to a huge active-user base (thousands of
    (recipient, channel) rows) and confirm the response is still
    capped."""
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(alert_service.MAX_ALERT_NOTIFICATIONS_LIMIT)
    ]

    result = alert_service.list_alert_notifications(db, alert_id=1, limit=50_000)

    chain.offset.return_value.limit.assert_called_once_with(
        alert_service.MAX_ALERT_NOTIFICATIONS_LIMIT
    )
    assert len(result) == alert_service.MAX_ALERT_NOTIFICATIONS_LIMIT


def test_list_alert_notifications_default_page_size():
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    alert_service.list_alert_notifications(db, alert_id=1)

    chain.offset.return_value.limit.assert_called_once_with(
        alert_service.DEFAULT_ALERT_NOTIFICATIONS_LIMIT
    )


# --- notification_service.list_notifications / mark_all_read --------------

def test_list_notifications_clamps_oversized_limit():
    db = MagicMock()
    user = MagicMock(id="user-1")
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(notification_service.MAX_NOTIFICATIONS_LIMIT)
    ]

    result = notification_service.list_notifications(db, user, limit=1_000_000)

    chain.offset.return_value.limit.assert_called_once_with(
        notification_service.MAX_NOTIFICATIONS_LIMIT
    )
    assert len(result) == notification_service.MAX_NOTIFICATIONS_LIMIT


def test_mark_all_read_is_not_capped_by_the_list_page_size():
    """Regression guard: mark_all_read must keep marking EVERY unread
    row in the retention window, not just one capped page of
    list_notifications - otherwise unread_count would stay stuck above
    zero after the user hits 'mark all read'. This asserts the
    underlying query is never bounded by .limit()/.offset() for this
    call path, on both a normal-sized and a large (5000-row) unread
    dataset."""
    for unread_count in (3, 5000):
        db = MagicMock()
        user = MagicMock(id="user-1")

        with patch.object(
            notification_service, "_notifications_query"
        ) as mock_query_builder:
            mock_query_builder.return_value.update.return_value = unread_count
            marked = notification_service.mark_all_read(db, user)

        # mark_all_read must go through the *unpaginated* query builder
        # directly (no .limit()/.offset() call recorded against it here).
        mock_query_builder.assert_called_once_with(db, user, unread_only=True)
        mock_query_builder.return_value.limit.assert_not_called()
        mock_query_builder.return_value.offset.assert_not_called()
        assert marked == unread_count


def test_mark_all_read_uses_a_single_bulk_update_not_orm_row_loading():
    """mark_all_read must not hydrate every matching Notification into
    the ORM session (.all() + per-row mutation) just to flip is_read/
    binned_at - that pulls thousands of full rows into Python, and
    autoflushes an UPDATE per row across the whole loop, turning a
    'mark all read' click into a long-held transaction. It must issue
    exactly one Query.update(synchronize_session=False) bulk UPDATE
    against the unpaginated query builder, then commit once - never
    call .all() at all, on either a small or a large (5000-row) unread
    dataset."""
    for unread_count in (3, 5000):
        db = MagicMock()
        user = MagicMock(id="user-1")

        with patch.object(
            notification_service, "_notifications_query"
        ) as mock_query_builder:
            mock_query_builder.return_value.update.return_value = unread_count
            marked = notification_service.mark_all_read(db, user)

        mock_query_builder.return_value.all.assert_not_called()
        mock_query_builder.return_value.update.assert_called_once()
        (values,), kwargs = mock_query_builder.return_value.update.call_args
        assert values[notification_service.Notification.is_read] is True
        assert notification_service.Notification.binned_at in values
        assert kwargs.get("synchronize_session") is False
        # One statement, one commit - not one commit per row.
        db.commit.assert_called_once()
        assert marked == unread_count


def test_mark_all_read_skips_the_websocket_push_when_nothing_was_unread():
    """No matched rows -> no NOTIFICATION_ALL_READ push (nothing
    changed for the client to sync)."""
    db = MagicMock()
    user = MagicMock(id="user-1")

    with patch.object(
        notification_service, "_notifications_query"
    ) as mock_query_builder, patch.object(
        notification_service, "manager"
    ) as mock_manager:
        mock_query_builder.return_value.update.return_value = 0
        marked = notification_service.mark_all_read(db, user)

    assert marked == 0
    mock_manager.notify_user.assert_not_called()


def test_unread_count_uses_a_count_query_not_a_row_fetch():
    """unread_count must stay a COUNT(*)-style query regardless of how
    many unread rows exist - it should never materialize the rows."""
    db = MagicMock()
    user = MagicMock(id="user-1")

    with patch.object(notification_service, "_notifications_query") as mock_query_builder:
        mock_query_builder.return_value.count.return_value = 123_456
        result = notification_service.unread_count(db, user)

    assert result == 123_456
    mock_query_builder.return_value.all.assert_not_called()


# --- analytics_service.prediction_insights (prediction history) -----------

def test_prediction_insights_clamps_oversized_limit():
    """Simulates a client requesting the entire prediction-history
    table (?limit=5000000) - must be capped at
    MAX_PREDICTION_INSIGHTS_LIMIT."""
    db = MagicMock()
    limit_call = (
        db.query.return_value.order_by.return_value.limit
    )
    limit_call.return_value.all.return_value = [
        MagicMock() for _ in range(analytics_service.MAX_PREDICTION_INSIGHTS_LIMIT)
    ]

    result = analytics_service.prediction_insights(db, limit=5_000_000)

    limit_call.assert_called_once_with(analytics_service.MAX_PREDICTION_INSIGHTS_LIMIT)
    assert len(result) == analytics_service.MAX_PREDICTION_INSIGHTS_LIMIT


def test_prediction_insights_default_limit_unchanged():
    """The existing default of 20 (what the frontend already relies
    on) must be untouched by the new cap."""
    db = MagicMock()
    limit_call = db.query.return_value.order_by.return_value.limit
    limit_call.return_value.all.return_value = []

    analytics_service.prediction_insights(db)

    limit_call.assert_called_once_with(20)


# --- prediction_service.forecast_demand (unbounded prediction loop) -------

def _fake_crowd_result():
    return {
        "predicted_count": 100,
        "confidence": 0.8,
        "target_datetime": "2026-01-01T00:00:00",
        "model_version": "random_forest_v1",
    }


def test_forecast_demand_clamps_absurd_hours_ahead():
    """A caller passing hours_ahead=1_000_000 (simulating an attempt
    to force a huge amount of per-hour model inference + DB writes,
    and an equally huge response) must only ever run
    MAX_DEMAND_FORECAST_HOURS_AHEAD iterations."""
    db = MagicMock()
    with patch.object(prediction_service, "_require_station"), \
         patch.object(
             prediction_service, "_cached_predict_crowd", return_value=_fake_crowd_result()
         ) as mock_predict:
        records = prediction_service.forecast_demand(db, station_id=1, hours_ahead=1_000_000)

    assert mock_predict.call_count == prediction_service.MAX_DEMAND_FORECAST_HOURS_AHEAD
    assert len(records) == prediction_service.MAX_DEMAND_FORECAST_HOURS_AHEAD
    assert db.commit.call_count == 1  # one batched commit, unchanged from before


def test_forecast_demand_normal_request_is_unaffected():
    """The frontend's real usage (hours_ahead=6 by default) must
    behave exactly as before - the cap should never bite for
    reasonable values."""
    db = MagicMock()
    with patch.object(prediction_service, "_require_station"), \
         patch.object(
             prediction_service, "_cached_predict_crowd", return_value=_fake_crowd_result()
         ) as mock_predict:
        records = prediction_service.forecast_demand(db, station_id=1, hours_ahead=6)

    assert mock_predict.call_count == 6
    assert len(records) == 6


def test_forecast_demand_non_positive_hours_ahead_floors_to_one():
    db = MagicMock()
    with patch.object(prediction_service, "_require_station"), \
         patch.object(
             prediction_service, "_cached_predict_crowd", return_value=_fake_crowd_result()
         ) as mock_predict:
        records = prediction_service.forecast_demand(db, station_id=1, hours_ahead=0)

    assert mock_predict.call_count == 1
    assert len(records) == 1
