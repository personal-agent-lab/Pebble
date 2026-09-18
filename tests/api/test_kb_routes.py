"""资料管理 HTTP 接口：浏览、搜索、读取与带版本校验的新建、保存、移动、删除。"""

from fastapi.testclient import TestClient

from server.main import create_app
from server.tools.personal_kb.service import KbStore
from tests.support.agent_double import FakeAgentGateway


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
            },
        )
        assert created.status_code == 201
        path = created.json()["path"]
        assert path == "kb/项目/验收.md"

        listed = client.get("/api/kb/documents", params={"directory": "项目"}).json()
        assert [item["path"] for item in listed["documents"]] == [path]

        document = client.get("/api/kb/document", params={"path": path}).json()
        assert document["title"] == "验收纪要"
        assert "tags" not in document
        assert document["body"] == "## 结果\n\n代号 CORAL-7421。"
        assert document["version"] == created.json()["version"]
        assert document["id"] == created.json()["id"]
        assert document["created_at"] and document["updated_at"]
        assert document["summary"] == ""

        described = client.post(
            "/api/kb/document/update",
            json={"path": path, "expected_version": document["version"], "summary": "二期验收结论"},
        )
        assert described.status_code == 200
        document = client.get("/api/kb/document", params={"path": path}).json()
        assert document["summary"] == "二期验收结论"
        listed = client.get("/api/kb/documents").json()["documents"]
        assert listed[0]["summary"] == "二期验收结论"

        hits = client.get("/api/kb/search", params={"q": "CORAL"}).json()["results"]
        assert hits[0]["path"] == path and "CORAL-7421" in hits[0]["snippet"]

        saved = client.post(
            "/api/kb/document/update",
            json={
                "path": path,
                "expected_version": document["version"],
                "body": "## 结果\n\n改过了。",
            },
        )
        assert saved.status_code == 200

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


def test_create_folder_and_document_inside_it(settings):
    with client_for(settings) as client:
        created = client.post("/api/kb/folders", json={"path": "课程/GSE"})
        assert created.status_code == 201
        assert created.json() == {"path": "kb/课程/GSE"}
        assert client.get("/api/kb/folders").json()["folders"] == ["课程", "课程/GSE"]

        duplicate = client.post("/api/kb/folders", json={"path": "课程/GSE"})
        assert duplicate.status_code == 422
        assert duplicate.json()["error"] == "invalid_kb"

        document = client.post(
            "/api/kb/documents",
            json={"title": "实验一", "body": "要求。", "directory": "课程/GSE"},
        )
        assert document.status_code == 201
        assert document.json()["path"] == "kb/课程/GSE/实验一.md"
        listed = client.get("/api/kb/documents").json()["documents"]
        assert listed[0]["updated_at"]


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


def test_summary_is_drafted_from_title_and_body_without_saving(settings):
    gateway = FakeAgentGateway()
    gateway.text = "“青铜项目周会：二期排期与负责人”"
    store = KbStore(settings.data_dir)
    with TestClient(create_app(gateway=gateway, kb_store=store)) as client:
        drafted = client.post(
            "/api/kb/summary", json={"title": "青铜周会", "body": "二期排期定在十月。"}
        )
        assert drafted.status_code == 200
        assert drafted.json() == {"summary": "青铜项目周会：二期排期与负责人"}
        assert "青铜周会" in gateway.text_calls[0]["text"]
        assert "二期排期定在十月。" in gateway.text_calls[0]["text"]
        assert store.list()["documents"] == []

        blank = client.post("/api/kb/summary", json={"title": "空", "body": "  "})
        assert blank.status_code == 422


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_image_is_saved_once_and_served_then_described_separately(settings):
    gateway = FakeAgentGateway()
    gateway.image_text = "  表格：第115师 [兵力] 15000余人\n第120师 14000余人 "
    store = KbStore(settings.data_dir)
    with TestClient(create_app(gateway=gateway, kb_store=store)) as client:
        uploaded = client.post("/api/kb/assets", files={"file": ("x.bin", PNG, "text/plain")})
        assert uploaded.status_code == 201
        path = uploaded.json()["path"]
        assert uploaded.json() == {"path": path}
        assert path.startswith("assets/") and path.endswith(".png")
        assert gateway.image_calls == []

        again = client.post("/api/kb/assets", files={"file": ("y.png", PNG, "image/png")})
        assert again.json()["path"] == path
        assert (settings.data_dir / "kb" / path).read_bytes() == PNG

        described = client.post("/api/kb/assets/describe", json={"path": path})
        assert described.status_code == 200
        assert described.json() == {
            "description": "表格：第115师 （兵力） 15000余人 第120师 14000余人"
        }
        assert gateway.image_calls[0]["mime_type"] == "image/png"
        assert (
            client.post("/api/kb/assets/describe", json={"path": "assets/none.png"}).status_code
            == 404
        )
        assert client.post("/api/kb/assets/describe", json={"path": "笔记.md"}).status_code == 422

        served = client.get(f"/api/kb/{path}")
        assert served.status_code == 200
        assert served.content == PNG
        assert served.headers["content-type"] == "image/png"
        assert served.headers["x-content-type-options"] == "nosniff"

        assert client.get("/api/kb/assets/missing.png").status_code == 404
        assert client.get("/api/kb/assets/..%2F.gitignore").status_code == 404
        assert "assets" not in client.get("/api/kb/folders").json()["folders"]


def test_image_upload_rejects_other_types_and_description_failure_is_empty(settings):
    gateway = FakeAgentGateway()
    gateway.image_text = RuntimeError("模型不可用")
    store = KbStore(settings.data_dir)
    with TestClient(create_app(gateway=gateway, kb_store=store)) as client:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        rejected = client.post("/api/kb/assets", files={"file": ("a.png", svg, "image/png")})
        assert rejected.status_code == 422

        path = client.post("/api/kb/assets", files={"file": ("a.png", PNG, "image/png")}).json()[
            "path"
        ]
        described = client.post("/api/kb/assets/describe", json={"path": path})
        assert described.json() == {"description": ""}

        gateway.image_text = "NO_IMAGE"
        described = client.post("/api/kb/assets/describe", json={"path": path})
        assert described.json() == {"description": ""}


def test_documents_and_folders_cannot_live_in_assets(settings):
    with client_for(settings) as client:
        for payload in (
            {"title": "t", "body": "b", "path": "assets/笔记"},
            {"title": "t", "body": "b", "directory": "assets"},
        ):
            assert client.post("/api/kb/documents", json=payload).status_code == 422
        assert client.post("/api/kb/folders", json={"path": "assets/sub"}).status_code == 422
