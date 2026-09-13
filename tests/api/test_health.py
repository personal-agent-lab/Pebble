from fastapi.testclient import TestClient

from server.config import Settings
from server.db import SCHEMA_VERSION
from server.main import app


def test_health_reports_data_dir_and_wal(settings: Settings) -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data_dir"] == str(settings.data_dir)
    assert payload["journal_mode"] == "wal"
    assert payload["schema_version"] == SCHEMA_VERSION
