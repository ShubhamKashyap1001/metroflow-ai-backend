from app.database.database import engine
try:
    with engine.connect() as connection:
        print("Connected Successfully")
except Exception as e:
    print(e)