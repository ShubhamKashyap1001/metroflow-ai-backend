from tests.conftest import NON_EXISTENT_ID


def test_get_news_feed_requires_auth(client):
    response = client.get("/api/v1/news/")
    assert response.status_code in (401, 403)


def test_create_news_requires_operator_role(client):
    payload = {"title": "Test Announcement", "content": "Test announcement content."}
    response = client.post("/api/v1/news/", json=payload)
    assert response.status_code in (401, 403)


def test_get_news_item_requires_auth(client):
    response = client.get(f"/api/v1/news/{NON_EXISTENT_ID}")
    assert response.status_code in (401, 403)


def test_update_news_requires_operator_role(client):
    response = client.patch(f"/api/v1/news/{NON_EXISTENT_ID}", json={"title": "Updated Title"})
    assert response.status_code in (401, 403)


def test_delete_news_requires_operator_role(client):
    response = client.delete(f"/api/v1/news/{NON_EXISTENT_ID}")
    assert response.status_code in (401, 403)