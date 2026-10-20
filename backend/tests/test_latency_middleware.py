from fastapi.testclient import TestClient

from app.main import create_app


def test_api_emits_server_timing_and_compresses_large_responses():
    app = create_app()

    @app.get("/_test/large-response")
    def large_response() -> dict[str, str]:
        return {"payload": "x" * 4096}

    response = TestClient(app).get(
        "/_test/large-response",
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["server-timing"].startswith("app;dur=")
    assert response.headers["content-encoding"] == "gzip"
