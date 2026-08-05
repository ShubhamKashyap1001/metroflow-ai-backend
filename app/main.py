from fastapi import FastAPI

app = FastAPI(
    title="MetroFlow API",
    version="1.0.0"
)

@app.get("/")
def home():
    return {
        "message": "MetroFlow Backend Running"
    }