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

            break
    return total
