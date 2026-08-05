from app.database.database import Base
from app.database.database import engine

import app.database.base

def create_database():
    Base.metadata.create_all(bind=engine)
    print("Database Tables Created Successfully")

if __name__ == "__main__":
    create_database()