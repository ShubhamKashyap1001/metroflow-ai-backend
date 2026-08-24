from sqlalchemy.orm import sessionmaker

from app.database.database import engine

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

def get_db():
    """One SQLAlchemy session per request.

    If the request raises (an unhandled exception, an HTTPException from
    deep in a service, etc.) with pending changes still on the session,
    that session was previously just closed - handing a dirty/half-
    written session back to the pool. Rolling back on any exception
    before closing guarantees the connection returns to the pool clean,
    same as autocommit=False already implies for the happy path.

    With expire_on_commit=False, objects returned to route handlers keep
    their attributes populated after commit (no surprise reload queries),
    while any session left dirty by an unhandled exception is still rolled
    back here before it is closed and returned to the pool.
    """
    db = SessionLocal()
    try:
        yield db
                                                                   
        if db.dirty or db.new or db.deleted:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
