import threading
import time

from fastapi.testclient import TestClient

from server.config import Settings
from server.db import SCHEMA_VERSION
from server.main import create_app
from server.tools.gmail.sync import GmailSource


def test_health_reports_data_dir_and_wal(settings: Settings) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data_dir"] == str(settings.data_dir)
    assert payload["journal_mode"] == "wal"
    assert payload["schema_version"] == SCHEMA_VERSION


def test_gmail_startup_failure_degrades_then_recovers(settings: Settings) -> None:
    allow_recovery = threading.Event()

    class TemporarilyOfflineGmail:
        def __init__(self) -> None:
            self.profile_calls = 0

        def get_profile(self) -> dict:
            self.profile_calls += 1
            if self.profile_calls == 1:
                raise ConnectionError("offline")
            assert allow_recovery.wait(timeout=1)
            return {"emailAddress": "owner@example.com", "historyId": "10"}

        def list_added_messages(self, history_id: str, page_token=None) -> dict:
            return {"historyId": history_id, "history": []}

    gmail = TemporarilyOfflineGmail()
    source = GmailSource(gmail, path=settings.data_dir / "gmail_sync.json", interval=0.01)

    with TestClient(create_app(mail_source=source)) as client:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            degraded = client.get("/api/health").json()
            if degraded["status"] == "degraded":
                break
            time.sleep(0.005)
        assert degraded["mail_source_error"] == "Gmail 检测失败（ConnectionError），游标未推进"

        allow_recovery.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            recovered = client.get("/api/health").json()
            if recovered["status"] == "ok" and gmail.profile_calls >= 2:
                break
            time.sleep(0.005)

        assert recovered["status"] == "ok"
        assert gmail.profile_calls == 2
