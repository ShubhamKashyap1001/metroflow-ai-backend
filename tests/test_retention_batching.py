
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.crowd_log import CrowdLog
from app.models.crowd_log_hourly import CrowdLogHourly
from app.models.notification import Notification
from app.utils.db_batch import batched_delete

import app.simulator.notification_bin_retention as notification_bin_retention
import app.simulator.retention as retention



def test_batched_delete_removes_a_large_backlog_in_bounded_batches():
    """A 12,050-row backlog with batch_size=5000 must come out as 3
    batches (5000, 5000, 2050) - never one query/delete that tries to
    handle the whole backlog at once - with a commit after each batch
    (so no single transaction ever holds locks against more than one
    batch's worth of rows)."""
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.limit.return_value.all.side_effect = [
        [(i,) for i in range(5000)],
        [(i,) for i in range(5000)],
        [(i,) for i in range(2050)],
    ]
    chain.delete.side_effect = [5000, 5000, 2050]

    total = batched_delete(
        db, CrowdLog, CrowdLog.id, CrowdLog.created_at < "cutoff", batch_size=5000
    )

    assert total == 12050
    # 3 fetches, each bounded to batch_size - never a query with no
    # LIMIT at all.
    assert chain.limit.call_count == 3
    chain.limit.assert_called_with(5000)
    assert chain.delete.call_count == 3
    # One commit per batch, not one commit for the whole backlog.
    assert db.commit.call_count == 3


def test_batched_delete_stops_after_final_partial_batch_without_requerying():
    """The batch that comes back smaller than batch_size is the last
    one - batched_delete must not issue a 4th (empty) fetch just to
    confirm there's nothing left."""
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.limit.return_value.all.side_effect = [
        [(i,) for i in range(5000)],
        [(i,) for i in range(2050)],
    ]
    chain.delete.side_effect = [5000, 2050]

    total = batched_delete(
        db, CrowdLog, CrowdLog.id, CrowdLog.created_at < "cutoff", batch_size=5000
    )

    assert total == 7050
    assert chain.limit.return_value.all.call_count == 2


def test_batched_delete_normal_small_dataset_is_a_single_batch():
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.limit.return_value.all.side_effect = [[(1,), (2,), (3,)]]
    chain.delete.side_effect = [3]

    total = batched_delete(
        db, CrowdLog, CrowdLog.id, CrowdLog.created_at < "cutoff", batch_size=5000
    )

    assert total == 3
    assert db.commit.call_count == 1


def test_batched_delete_nothing_matches_does_nothing():
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.limit.return_value.all.side_effect = [[]]

    total = batched_delete(
        db, CrowdLog, CrowdLog.id, CrowdLog.created_at < "cutoff", batch_size=5000
    )

    assert total == 0
    chain.delete.assert_not_called()
    db.commit.assert_not_called()



def _fake_bucket_row(i):
    return SimpleNamespace(
        station_id=i,
        hour_bucket=f"hour-{i}",
        avg_count=5.0,
        max_count=10,
        min_count=1,
        sample_count=3,
    )


def test_rollup_paginates_a_large_bucket_backlog_instead_of_loading_it_all():
    """A rollup backlog of 1,500 (station, hour) buckets with
    CROWD_ROLLUP_BATCH_SIZE=1000 must be fetched/upserted as 2 pages
    (1000, 500), each page committed before the next page is even
    queried - the aggregate SELECT is never pulled in with a single
    unbounded `.all()`."""
    db = MagicMock()
    order_by_mock = (
        db.query.return_value.filter.return_value.group_by.return_value.order_by.return_value
    )
    page1 = [_fake_bucket_row(i) for i in range(1000)]
    page2 = [_fake_bucket_row(i) for i in range(1000, 1500)]
    order_by_mock.limit.return_value.offset.return_value.all.side_effect = [page1, page2]

    with patch.object(retention, "batched_delete", return_value=0) as mock_batched_delete, \
         patch.object(retention.settings, "CROWD_ROLLUP_BATCH_SIZE", 1000), \
         patch.object(retention, "_upsert_hourly_batch") as mock_upsert:
        stats = retention._rollup_and_delete(db)

    assert stats["rolled_up_buckets"] == 1500
    # Two pages upserted (never the whole 1500-row backlog in one go).
    assert mock_upsert.call_count == 2
    assert mock_upsert.call_args_list[0].args[1] == page1
    assert mock_upsert.call_args_list[1].args[1] == page2
    # Bounded LIMIT on every fetch, moving OFFSET, one commit per page.
    assert order_by_mock.limit.call_args_list == [((1000,),), ((1000,),)]
    assert order_by_mock.limit.return_value.offset.call_args_list == [
        ((0,),),
        ((1000,),),
    ]
    assert db.commit.call_count == 2
    # The raw-row delete for the same cutoff also goes through the
    # shared batching helper, not a bare .delete().
    mock_batched_delete.assert_called_once()
    assert mock_batched_delete.call_args.args[1] is retention.CrowdLog
    assert mock_batched_delete.call_args.args[2] is retention.CrowdLog.id
    assert (
        mock_batched_delete.call_args.kwargs["batch_size"]
        == retention.settings.RETENTION_BATCH_SIZE
    )


def test_rollup_normal_small_backlog_is_a_single_page():
    db = MagicMock()
    order_by_mock = (
        db.query.return_value.filter.return_value.group_by.return_value.order_by.return_value
    )
    page = [_fake_bucket_row(i) for i in range(3)]
    order_by_mock.limit.return_value.offset.return_value.all.side_effect = [page]

    with patch.object(retention, "batched_delete", return_value=0), \
         patch.object(retention, "_upsert_hourly_batch") as mock_upsert:
        stats = retention._rollup_and_delete(db)

    assert stats["rolled_up_buckets"] == 3
    assert mock_upsert.call_count == 1
    assert db.commit.call_count == 1


def test_rollup_no_backlog_upserts_nothing():
    db = MagicMock()
    order_by_mock = (
        db.query.return_value.filter.return_value.group_by.return_value.order_by.return_value
    )
    order_by_mock.limit.return_value.offset.return_value.all.side_effect = [[]]

    with patch.object(retention, "batched_delete", return_value=0), \
         patch.object(retention, "_upsert_hourly_batch") as mock_upsert:
        stats = retention._rollup_and_delete(db)

    assert stats["rolled_up_buckets"] == 0
    mock_upsert.assert_not_called()
    db.commit.assert_not_called()



def test_hard_delete_stale_raw_goes_through_the_batching_helper():
    db = MagicMock()
    with patch.object(retention, "batched_delete", return_value=9001) as mock_bd:
        result = retention._hard_delete_stale_raw(db)

    assert result == 9001
    args = mock_bd.call_args.args
    assert args[0] is db
    assert args[1] is retention.CrowdLog
    assert args[2] is retention.CrowdLog.id
    assert mock_bd.call_args.kwargs["batch_size"] == retention.settings.RETENTION_BATCH_SIZE


def test_hard_delete_stale_hourly_goes_through_the_batching_helper():
    db = MagicMock()
    with patch.object(retention, "batched_delete", return_value=123) as mock_bd:
        result = retention._hard_delete_stale_hourly(db)

    assert result == 123
    args = mock_bd.call_args.args
    assert args[0] is db
    assert args[1] is retention.CrowdLogHourly
    assert args[2] is retention.CrowdLogHourly.id
    assert mock_bd.call_args.kwargs["batch_size"] == retention.settings.RETENTION_BATCH_SIZE


def test_notification_bin_retention_goes_through_the_batching_helper():
    db = MagicMock()
    with patch.object(
        notification_bin_retention, "batched_delete", return_value=4321
    ) as mock_bd:
        stats = notification_bin_retention.run_retention_once(db)

    assert stats == {"binned_notifications_deleted": 4321}
    args = mock_bd.call_args.args
    assert args[0] is db
    assert args[1] is notification_bin_retention.Notification
    assert args[2] is notification_bin_retention.Notification.id
    assert (
        mock_bd.call_args.kwargs["batch_size"]
        == notification_bin_retention.settings.RETENTION_BATCH_SIZE
    )


def test_notification_bin_retention_large_backlog_end_to_end_through_batched_delete():
    """End-to-end (without mocking batched_delete itself): a 12,050-row
    binned backlog is removed in bounded batches with a commit after
    each one, and the job's returned count reflects the true total."""
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.limit.return_value.all.side_effect = [
        [(i,) for i in range(5000)],
        [(i,) for i in range(5000)],
        [(i,) for i in range(2050)],
    ]
    chain.delete.side_effect = [5000, 5000, 2050]

    with patch.object(
        notification_bin_retention.settings, "RETENTION_BATCH_SIZE", 5000
    ):
        stats = notification_bin_retention.run_retention_once(db)

    assert stats == {"binned_notifications_deleted": 12050}
    assert db.commit.call_count == 3
