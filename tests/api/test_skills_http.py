"""技能管理 HTTP 接口：目录、创建编辑、状态切换、版本、变更审批与加载记录。"""

import json
import time

from fastapi.testclient import TestClient

from server.main import create_app
from server.skills.service import SkillService
from tests.support.agent_double import FakeAgentGateway


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """等后台轮次进入网关替身；轮次在请求返回后才执行。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def client_for(settings) -> TestClient:
    return TestClient(create_app(skills=SkillService(settings.data_dir, settings.db_path)))


def create(client: TestClient, skill_id: str = "weekly-report", **fields) -> dict:
    payload = {
        "skill_id": skill_id,
        "name": "周报整理",
        "description": "按固定分节整理本周周报",
        "body": "## 步骤\n\n1. 先查日历\n",
    }
    payload.update(fields)
    response = client.post("/api/skills", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_detail_update_and_conflict(settings):
    with client_for(settings) as client:
        created = create(client)
        assert created["status"] == "applied"

        detail = client.get("/api/skills/weekly-report")
        assert detail.status_code == 200
        body = detail.json()
        assert body["origin"] == "user"
        assert body["managed"] is False
        assert body["state"] == "active"
        assert body["revision"] and body["usage"] == []

        # 修改需要 expected_revision；过期版本 409 并附当前版本。
        updated = client.put(
            "/api/skills/weekly-report",
            json={"expected_revision": body["revision"], "body": "新正文\n"},
        )
        assert updated.status_code == 200
        current = updated.json()["skill"]["revision"]
        stale = client.put(
            "/api/skills/weekly-report",
            json={"expected_revision": body["revision"], "body": "并发写入\n"},
        )
        assert stale.status_code == 409
        assert stale.json()["current_revision"] == current

        # 不存在的技能 404；非法标识 422。
        assert client.get("/api/skills/missing").status_code == 404
        invalid = client.post(
            "/api/skills",
            json={
                "skill_id": "Bad Id",
                "name": "x",
                "description": "d",
                "body": "b",
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"] == "invalid_skill"


def test_list_filter_archive_and_managed(settings):
    with client_for(settings) as client:
        create(client, "live-one")
        create(client, "other-one")
        listed = client.get("/api/skills", params={"q": "live"}).json()
        assert [entry["skill_id"] for entry in listed] == ["live-one"]

        assert client.post("/api/skills/live-one/archive").status_code == 204
        assert [entry["skill_id"] for entry in client.get("/api/skills").json()] == ["other-one"]
        archived = client.get("/api/skills", params={"state": "archived"}).json()
        assert [entry["skill_id"] for entry in archived] == ["live-one"]

        restored = client.post("/api/skills/live-one/restore").json()
        assert restored["state"] == "active"

        # 管理页只能取消直写权：用户手写的技能恒为受保护（契约 §2）。
        protected = client.post("/api/skills/live-one/managed", json={"value": True})
        assert protected.status_code == 422
        assert protected.json()["error"] == "invalid_skill"
        assert client.get("/api/skills/live-one").json()["managed"] is False
        assert client.post("/api/skills/live-one/managed", json={"value": False}).json() == {
            "skill_id": "live-one",
            "managed": False,
        }
        # 管理策略不影响目录与内容版本。
        assert [entry["skill_id"] for entry in client.get("/api/skills").json()] == [
            "live-one",
            "other-one",
        ]


def test_versions_and_restore(settings):
    with client_for(settings) as client:
        create(client)
        first = client.get("/api/skills/weekly-report").json()["revision"]
        client.put(
            "/api/skills/weekly-report",
            json={"expected_revision": first, "body": "第二版\n"},
        )
        versions = client.get("/api/skills/weekly-report/versions").json()
        assert len(versions) == 2
        assert versions[0]["actor"] == "user"
        assert versions[0]["change_id"]

        detail = client.get(f"/api/skills/weekly-report/versions/{first}").json()
        assert "先查日历" in detail["body"]

        restored = client.post(
            "/api/skills/weekly-report/restore-version", json={"revision": first}
        )
        assert restored.status_code == 200
        assert "先查日历" in client.get("/api/skills/weekly-report").json()["body"]
        assert client.get("/api/skills/weekly-report/versions/does-not-exist").status_code == 404


def test_change_approval_flow(settings):
    service = SkillService(settings.data_dir, settings.db_path)
    with client_for(settings) as client:
        create(client)
        # 直接造一条复盘建议（A+B 阶段没有复盘生产者）。
        proposed = service.record_change(
            _change_request(
                payload={"body": "复盘建议的正文\n"}, skill_id="weekly-report", service=service
            )
        )
        assert proposed["status"] == "proposed"

        listed = client.get("/api/skill-changes", params={"status": "proposed"}).json()
        assert [change["id"] for change in listed] == [proposed["id"]]
        view = listed[0]
        assert view["payload"]["body"] == "复盘建议的正文\n"
        assert view["actor"] == "review"
        assert view["evidence_item_ids"] == []

        approved = client.post(
            f"/api/skill-changes/{proposed['id']}/approve",
            json={"expected_revision": proposed["base_revision"]},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "applied"
        assert client.get("/api/skills/weekly-report").json()["body"] == "复盘建议的正文\n"

        rejected_view = service.record_change(
            _change_request(
                payload={"body": "被拒绝的正文\n"}, skill_id="weekly-report", service=service
            )
        )
        assert client.post(f"/api/skill-changes/{rejected_view['id']}/reject").status_code == 204
        assert client.get("/api/skill-changes", params={"status": "rejected"}).json()
        assert "被拒绝" not in client.get("/api/skills/weekly-report").json()["body"]


def _change_request(*, payload: dict, skill_id: str, service: SkillService):
    from server.skills.models import ChangeAction, ChangeActor
    from server.skills.service import ChangeRequest

    return ChangeRequest(
        action=ChangeAction.PATCH,
        payload=payload,
        actor=ChangeActor.REVIEW,
        reason="复盘建议",
        skill_id=skill_id,
        base_revision=service.get(skill_id).revision,
    )


def test_attachments_write_read_remove(settings):
    with client_for(settings) as client:
        created = create(client, "with-files", attachments={"references/notes.md": "旧内容\n"})
        revision = client.get("/api/skills/with-files").json()["revision"]
        assert [item["path"] for item in created["skill"]["files"]] == ["references/notes.md"]

        read = client.get("/api/skills/with-files/files/references/notes.md")
        assert read.status_code == 200
        assert read.json()["content"] == "旧内容\n"
        # 不存在的附件与越界路径都是 404。
        assert client.get("/api/skills/with-files/files/references/missing.md").status_code == 404
        assert client.get("/api/skills/with-files/files/../../etc").status_code == 404

        stale = client.post(
            "/api/skills/with-files/files",
            json={
                "relative_path": "templates/report.md",
                "content": "模板\n",
                "expected_revision": "0" * 64,
            },
        )
        assert stale.status_code == 409
        written = client.post(
            "/api/skills/with-files/files",
            json={
                "relative_path": "templates/report.md",
                "content": "模板\n",
                "expected_revision": revision,
            },
        )
        assert written.status_code == 200
        assert written.json()["status"] == "applied"
        paths = {item["path"] for item in client.get("/api/skills/with-files").json()["files"]}
        assert paths == {"references/notes.md", "templates/report.md"}

        removed = client.post(
            "/api/skills/with-files/files/remove",
            json={
                "relative_path": "references/notes.md",
                "expected_revision": written.json()["skill"]["revision"],
            },
        )
        assert removed.status_code == 200
        paths = {item["path"] for item in client.get("/api/skills/with-files").json()["files"]}
        assert paths == {"templates/report.md"}
        # 越界附件路径 422。
        invalid = client.post(
            "/api/skills/with-files/files",
            json={
                "relative_path": "../outside.md",
                "content": "x",
                "expected_revision": removed.json()["skill"]["revision"],
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"] == "invalid_skill"

        # 删除不存在的附件按契约 §11 是 404。
        missing = client.post(
            "/api/skills/with-files/files/remove",
            json={
                "relative_path": "references/gone.md",
                "expected_revision": removed.json()["skill"]["revision"],
            },
        )
        assert missing.status_code == 404
        assert missing.json()["error"] == "unknown_skill"

        # 内容没变的重复写入不产生提交，也不该报 503。
        repeat = client.post(
            "/api/skills/with-files/files",
            json={
                "relative_path": "templates/report.md",
                "content": "模板\n",
                "expected_revision": removed.json()["skill"]["revision"],
            },
        )
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "applied"


def test_update_validates_frontmatter(settings):
    """PUT 走 patch，字段规则与创建一致：名称非空、描述不超 160、正文非空（契约 §2）。"""

    with client_for(settings) as client:
        create(client)
        revision = client.get("/api/skills/weekly-report").json()["revision"]
        for payload in (
            {"name": "  "},
            {"description": "长" * 161},
            {"body": "   "},
        ):
            failed = client.put(
                "/api/skills/weekly-report",
                json={"expected_revision": revision, **payload},
            )
            assert failed.status_code == 422, failed.text
            assert failed.json()["error"] == "invalid_skill"
        # 未通过校验的变更不落盘，也不留下待审记录。
        assert client.get("/api/skills/weekly-report").json()["revision"] == revision
        assert client.get("/api/skill-changes", params={"status": "proposed"}).json() == []


def test_invalid_skill_id_is_rejected(settings):
    """非法标识在读路径是 404，在写路径是 422，都不会变成 500。"""

    with client_for(settings) as client:
        assert client.get("/api/skills/..%2Fescape").status_code == 404
        assert client.get("/api/skills/Bad%20Id").status_code == 404
        for path in ("/api/skills/Bad%20Id/archive", "/api/skills/Bad%20Id/versions"):
            assert client.post(path).status_code in (404, 405)
        written = client.post(
            "/api/skills/Bad%20Id/files",
            json={
                "relative_path": "references/a.md",
                "content": "x",
                "expected_revision": "0" * 64,
            },
        )
        assert written.status_code == 422
        assert written.json()["error"] == "invalid_skill"
        assert client.get("/api/skill-changes").json() == []


def test_task_skill_usage(settings):
    from server.sessions.service import SessionStore

    with client_for(settings) as client:
        create(client, "loaded-one")
        skill = client.get("/api/skills/loaded-one").json()
        service = SkillService(settings.data_dir, settings.db_path)
        tasks = SessionStore()
        task = tasks.create_task("技能加载验收")
        service.record_load(task["task_id"], "run-x", service.get("loaded-one"), "manual")
        usage = client.get(f"/api/tasks/{task['task_id']}/skill-usage").json()
        assert [(row["skill_id"], row["source"], row["revision"]) for row in usage] == [
            ("loaded-one", "manual", skill["revision"])
        ]
        assert client.get("/api/tasks/missing/skill-usage").status_code == 404


def test_message_selection_payload_and_validation(settings):
    """multipart `selection` 按契约 §6 形状解析：只带标识，非法与越界在选择期就拒绝。"""

    gateway = FakeAgentGateway()
    service = SkillService(settings.data_dir, settings.db_path)
    app = create_app(gateway=gateway, skills=service)
    selection = {
        "skills": [{"id": "weekly-report"}],
        "excluded_skill_ids": ["meeting-notes"],
        "auto_match": False,
    }
    with TestClient(app) as client:
        create(client, "weekly-report")
        create(client, "meeting-notes")
        created = client.post(
            "/api/tasks",
            data={"model": "auto", "message": "整理周报", "selection": json.dumps(selection)},
        )
        assert created.status_code == 201, created.text
        assert wait_for(lambda: gateway.calls_of("message"))
        call = gateway.calls_of("message")[0]
        assert list(call["skills"]) == ["weekly-report"]
        assert list(call["excluded_skill_ids"]) == ["meeting-notes"]
        assert call["auto_match"] is False

        # 既有任务追加消息同样携带选择。
        task_id = created.json()["task"]["task_id"]
        appended = client.post(
            f"/api/tasks/{task_id}/messages",
            data={"message": "继续", "selection": json.dumps(selection)},
        )
        assert appended.status_code == 202, appended.text
        assert wait_for(lambda: len(gateway.calls_of("message")) >= 2)

        # 不存在的技能按契约 §11 是 404，且不留下半个任务。
        unknown = client.post(
            "/api/tasks",
            data={
                "model": "auto",
                "message": "x",
                "selection": json.dumps({**selection, "skills": [{"id": "missing"}]}),
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"] == "unknown_skill"

        # 超过 10 个（去重后计数）、非契约形状与坏 JSON 都是 422。
        for index in range(11):
            create(client, f"filler-{index:02d}")
        many = {**selection, "skills": [{"id": f"filler-{i:02d}"} for i in range(11)]}
        over = client.post(
            "/api/tasks",
            data={"model": "auto", "message": "x", "selection": json.dumps(many)},
        )
        assert over.status_code == 422
        assert over.json()["errors"][0]["field"] == "skills"
        # 重复项去重后不触发上限。
        repeated = {**selection, "skills": [{"id": "weekly-report"} for _ in range(11)]}
        assert (
            client.post(
                "/api/tasks",
                data={"model": "auto", "message": "x", "selection": json.dumps(repeated)},
            ).status_code
            == 201
        )
        for bad in ('{"skills": ["weekly-report"]}', "{not json"):
            failed = client.post(
                "/api/tasks",
                data={"model": "auto", "message": "x", "selection": bad},
            )
            assert failed.status_code == 422, failed.text
            assert failed.json()["error"] == "invalid_skill"
        assert len(client.get("/api/tasks").json()) == 2


def test_selection_without_skill_service_is_rejected(settings):
    """没有装配技能服务时，带选择的消息报依赖不可用，不静默丢掉用户的选择。"""

    app = create_app(gateway=FakeAgentGateway())
    with TestClient(app) as client:
        response = client.post(
            "/api/tasks",
            data={
                "model": "auto",
                "message": "x",
                "selection": json.dumps({"skills": [{"id": "weekly-report"}]}),
            },
        )
        assert response.status_code == 503
