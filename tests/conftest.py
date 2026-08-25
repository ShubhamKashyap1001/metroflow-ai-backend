import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)

INVALID_BEARER = {"Authorization": "Bearer not-a-real-jwt.abc.def"}

NON_EXISTENT_ID = 999_999_999