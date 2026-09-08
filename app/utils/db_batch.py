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
