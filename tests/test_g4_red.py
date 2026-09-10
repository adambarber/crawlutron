from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_overlong_scope_regex_returns_422():
    resp = client.post("/v1/crawl", json={
        "url": "http://example.com/page",
        "policy": {
            "scope_regex": "a" * 300,
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "limits": {"timeout_seconds": 5, "max_retries": 0},
        },
    })
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text[:200]}"
