from sqlalchemy.orm import sessionmaker

from app.database.database import engine

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

def get_db():
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
