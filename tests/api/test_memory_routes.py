"""记忆管理 HTTP 接口：读取当前记忆、带版本校验的整份保存。"""

from fastapi.testclient import TestClient

from server.main import create_app
from server.memory.service import MemoryStore
from tests.support.agent_double import FakeAgentGateway


def client_for(settings) -> TestClient:
    return TestClient(
        create_app(gateway=FakeAgentGateway(), memory_store=MemoryStore(settings.data_dir))
    )


def test_read_and_write_whole_document(settings):
    with client_for(settings) as client:
        current = client.get("/api/memory").json()
        assert current["user"] == {
            "content": "",
            "usage": {"chars": 0, "limit": 1375},
            "version": current["user"]["version"],
        }
        assert current["memory"]["usage"] == {"chars": 0, "limit": 2200}

        document = "## 表达\n\n- 回答先给结论\n- 默认使用中文\n"
        saved = client.put(
            "/api/memory/user",
            json={"content": document, "expected_version": current["user"]["version"]},
        )
        assert saved.status_code == 200
        body = saved.json()
        assert (body["target"], body["changed"], body["content"]) == (
            "user",
            True,
            document.strip(),
        )
        assert body["usage"]["chars"] == len(document.strip())

        unchanged = client.put(
            "/api/memory/user", json={"content": document, "expected_version": body["version"]}
        ).json()
        assert unchanged["changed"] is False

        cleared = client.put(
            "/api/memory/user", json={"content": "", "expected_version": body["version"]}
        ).json()
        assert cleared["content"] == ""
        assert client.get("/api/memory").json()["user"]["version"] == cleared["version"]
        assert (settings.data_dir / "memory" / "USER.md").read_text() == ""


def test_stale_version_conflicts_without_writing(settings):
    with client_for(settings) as client:
        stale = client.get("/api/memory").json()["memory"]["version"]
        (settings.data_dir / "memory" / "MEMORY.md").write_text("编辑器写入", encoding="utf-8")

        response = client.put(
            "/api/memory/memory", json={"content": "页面内容", "expected_version": stale}
        )
        assert response.status_code == 409
        assert response.json()["error"] == "version_conflict"
        assert client.get("/api/memory").json()["memory"]["content"] == "编辑器写入"


def test_capacity_and_unknown_target(settings):
    with client_for(settings) as client:
        version = client.get("/api/memory").json()["user"]["version"]

        full = client.put(
            "/api/memory/user", json={"content": "甲" * 1376, "expected_version": version}
        )
        assert full.status_code == 422
        assert full.json() == {
            "error": "memory_full",
            "message": "“关于你”放不下：保存后需要 1376 个字符，上限为 1375",
            "target": "user",
            "used": 1376,
            "limit": 1375,
        }

        unknown = client.put(
            "/api/memory/other", json={"content": "x", "expected_version": version}
        )
        assert unknown.status_code == 422


def test_oversized_file_is_still_readable(settings):
    with client_for(settings) as client:
        (settings.data_dir / "memory" / "USER.md").write_text("甲" * 1400, encoding="utf-8")
        user = client.get("/api/memory").json()["user"]
        assert user["usage"] == {"chars": 1400, "limit": 1375}
        assert user["content"] == "甲" * 1400
