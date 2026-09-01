"""Shared helper for retention/rollup background jobs: delete matching
rows in bounded batches instead of one unbounded DELETE.

Postgres has no `DELETE ... LIMIT`, so batching a delete means
repeatedly deleting the primary keys matched by a bounded subquery
until none are left, committing after each batch. Two things this
buys over a single `query.filter(...).delete()`:

  - Memory: each pass only ever pulls `batch_size` primary keys into
    Python, never the full set of ids that match the retention
    cutoff - a job that's fallen behind (or a large deployment) can
    have a backlog of hundreds of thousands of stale rows and this
    still only ever holds `batch_size` ints in memory at a time.
  - Transaction length: each batch commits (and releases its row
    locks) before the next one starts, instead of one DELETE holding
    locks against every matching row for however long it takes to
    remove the *entire* backlog - other writers to the same table
    (e.g. the simulator's own INSERTs) don't queue up behind a single
    long-running retention transaction.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

DEFAULT_RETENTION_BATCH_SIZE = 5000


def batched_delete(
    db: Session,
    model,
    id_column,
    filter_clause,
    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE,
) -> int:
    """Delete every row of `model` matching `filter_clause`, one
    bounded batch (and one commit) at a time. Returns the total number
    of rows deleted across all batches."""
    total = 0
    while True:
        batch_ids = (
            db.query(id_column)
            .filter(filter_clause)
            .limit(batch_size)
            .all()
        )
        if not batch_ids:
            break
        ids = [row[0] for row in batch_ids]
        deleted = (
            db.query(model)
            .filter(id_column.in_(ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        total += deleted
        if len(ids) < batch_size:
            # Fewer than a full batch came back - this was the last
            # one, no need to run another (now-empty) query.
            break
    return total
