"""记忆管理 HTTP 接口：读取当前记忆、带版本校验的新增、修改与删除。"""

from fastapi.testclient import TestClient

from server.main import create_app
from server.memory.service import MemoryStore
from tests.support.agent_double import FakeAgentGateway


def client_for(settings) -> TestClient:
    return TestClient(
        create_app(gateway=FakeAgentGateway(), memory_store=MemoryStore(settings.data_dir))
    )


def test_read_add_update_and_remove_entries(settings):
    with client_for(settings) as client:
        current = client.get("/api/memory").json()
        assert current["user"] == {
            "entries": [],
            "usage": {"chars": 0, "limit": 1375},
            "version": current["user"]["version"],
        }
        assert current["memory"]["usage"] == {"chars": 0, "limit": 2200}

        added = client.post(
            "/api/memory/entries/add",
            json={
                "target": "user",
                "content": "回答先给结论",
                "expected_version": current["user"]["version"],
            },
        )
        assert added.status_code == 200
        body = added.json()
        assert (body["target"], body["changed"], body["entries"]) == (
            "user",
            True,
            ["回答先给结论"],
        )

        updated = client.post(
            "/api/memory/entries/update",
            json={
                "target": "user",
                "old": "回答先给结论",
                "content": "回答先解释推导",
                "expected_version": body["version"],
            },
        ).json()
        assert updated["entries"] == ["回答先解释推导"]

        removed = client.post(
            "/api/memory/entries/remove",
            json={
                "target": "user",
                "old": "回答先解释推导",
                "expected_version": updated["version"],
            },
        ).json()
        assert removed["entries"] == []
        assert client.get("/api/memory").json()["user"]["version"] == removed["version"]
        assert (settings.data_dir / "memory" / "USER.md").read_text() == ""


def test_stale_version_conflicts_without_writing(settings):
    with client_for(settings) as client:
        stale = client.get("/api/memory").json()["memory"]["version"]
        (settings.data_dir / "memory" / "MEMORY.md").write_text("编辑器写入", encoding="utf-8")

        response = client.post(
            "/api/memory/entries/add",
            json={"target": "memory", "content": "页面新增", "expected_version": stale},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "version_conflict"
        assert client.get("/api/memory").json()["memory"]["entries"] == ["编辑器写入"]


def test_validation_and_capacity_errors(settings):
    with client_for(settings) as client:
        version = client.get("/api/memory").json()["user"]["version"]

        missing = client.post(
            "/api/memory/entries/remove",
            json={"target": "user", "old": "不存在", "expected_version": version},
        )
        assert missing.status_code == 422
        assert missing.json()["error"] == "invalid_memory"

        full = client.post(
            "/api/memory/entries/add",
            json={"target": "user", "content": "甲" * 1376, "expected_version": version},
        )
        assert full.status_code == 422
        assert full.json() == {
            "error": "memory_full",
            "message": "“关于你”放不下：保存后需要 1376 个字符，上限为 1375",
            "target": "user",
            "used": 1376,
            "limit": 1375,
        }

        unknown = client.post(
            "/api/memory/entries/add",
            json={"target": "other", "content": "x", "expected_version": version},
        )
        assert unknown.status_code == 422


def test_oversized_file_is_still_listed(settings):
    with client_for(settings) as client:
        (settings.data_dir / "memory" / "USER.md").write_text("甲" * 1400, encoding="utf-8")
        user = client.get("/api/memory").json()["user"]
        assert user["usage"] == {"chars": 1400, "limit": 1375}
        assert user["entries"] == ["甲" * 1400]
