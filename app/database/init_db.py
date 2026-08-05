from app.database.base import Base
from app.database.database import engine

# Import all models
from app.models import *


def create_tables():
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    create_tables()
    print("✅ Database Tables Created Successfully")