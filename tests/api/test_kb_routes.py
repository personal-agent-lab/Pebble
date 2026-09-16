"""资料管理 HTTP 接口：浏览、搜索、读取与带版本校验的新建、保存、移动、删除。"""

from fastapi.testclient import TestClient

from server.main import create_app
from server.tools.personal_kb.service import KbStore


def client_for(settings) -> TestClient:
    return TestClient(create_app(kb_store=KbStore(settings.data_dir)))


def test_browse_create_read_save_move_and_delete(settings):
    with client_for(settings) as client:
        created = client.post(
            "/api/kb/documents",
            json={
                "title": "验收纪要",
                "body": "## 结果\n\n代号 CORAL-7421。",
                "path": "项目/验收",
                "tags": ["项目"],
            },
        )
        assert created.status_code == 201
        path = created.json()["path"]
        assert path == "kb/项目/验收.md"

        listed = client.get("/api/kb/documents", params={"directory": "项目"}).json()
        assert [item["path"] for item in listed["documents"]] == [path]

        document = client.get("/api/kb/document", params={"path": path}).json()
        assert document["title"] == "验收纪要"
        assert document["tags"] == ["项目"]
        assert document["body"] == "## 结果\n\n代号 CORAL-7421。"
        assert document["version"] == created.json()["version"]
        assert document["id"] == created.json()["id"]
        assert document["created_at"] and document["updated_at"]

        hits = client.get("/api/kb/search", params={"q": "CORAL"}).json()["results"]
        assert hits[0]["path"] == path and "CORAL-7421" in hits[0]["snippet"]

        saved = client.post(
            "/api/kb/document/update",
            json={
                "path": path,
                "expected_version": document["version"],
                "body": "## 结果\n\n改过了。",
                "tags": [],
            },
        )
        assert saved.status_code == 200
        assert client.get("/api/kb/document", params={"path": path}).json()["tags"] == []

        # 旧版本保存被拒绝，不覆盖
        stale = client.post(
            "/api/kb/document/update",
            json={"path": path, "expected_version": document["version"], "body": "过期的写入"},
        )
        assert stale.status_code == 409
        assert stale.json()["current_version"] == saved.json()["version"]

        moved = client.post(
            "/api/kb/document/move",
            json={
                "path": path,
                "expected_version": saved.json()["version"],
                "new_path": "归档/验收",
            },
        ).json()
        assert moved["path"] == "kb/归档/验收.md"
        assert client.get("/api/kb/document", params={"path": path}).status_code == 404

        deleted = client.post(
            "/api/kb/document/delete",
            json={"path": moved["path"], "expected_version": moved["version"]},
        )
        assert deleted.status_code == 200
        assert client.get("/api/kb/documents").json()["documents"] == []


def test_invalid_input_and_missing_store_are_reported(settings):
    with client_for(settings) as client:
        empty = client.post("/api/kb/documents", json={"title": " ", "body": "正文"})
        assert empty.status_code == 422
        assert empty.json()["error"] == "invalid_kb"
        outside = client.get("/api/kb/document", params={"path": "../x.md"})
        assert outside.status_code == 422
        assert client.get("/api/kb/document", params={"path": "missing.md"}).status_code == 404

    with TestClient(create_app()) as client:
        response = client.get("/api/kb/documents")
        assert response.status_code == 503
        assert response.json()["error"] == "unavailable"
