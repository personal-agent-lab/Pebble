from fastapi.testclient import TestClient

from server.main import create_app
from server.skills import service


def test_skill_http_lifecycle(settings):
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/skills", json=dict(name="简洁汇报", description="精简结果", body="先说结果")
        )
        assert response.status_code == 201
        skill = response.json()
        assert skill["content_hash"]
        sid = skill["id"]
        assert (
            client.patch(
                f"/api/skills/{sid}", json=dict(expected_revision="old", body="x")
            ).status_code
            == 409
        )
        assert (
            client.patch(
                f"/api/skills/{sid}", json=dict(expected_revision=skill["content_hash"], name="")
            ).status_code
            == 422
        )
        assert client.post(f"/api/skills/{sid}/disable").status_code == 200
        assert client.get("/api/skills?status=disabled").json()[0]["id"] == sid
        assert client.post(f"/api/skills/{sid}/enable").status_code == 200
        draft = service.create_draft(skill_id=sid, name="更简洁", description="说明", body="一句话")
        assert client.get("/api/skill-drafts").json()[0]["draft_id"] == draft.draft_id
        url = f"/api/skill-drafts/{draft.draft_id}/approve"
        assert client.post(url, json=dict(expected_revision="old")).status_code == 409
        assert (
            client.post(url, json=dict(expected_revision=draft.skill.content_hash)).status_code
            == 200
        )
        assert client.get("/api/skill-drafts").json() == []
        assert client.get(f"/api/skills/{sid}/versions").json()[0]["body"] == "一句话"
        assert client.post(f"/api/skills/{sid}/archive").status_code == 204
        assert client.get("/api/skills?status=archived").json()[0]["id"] == sid


def test_skills_selection_survives_multipart_create_and_reply(settings):
    import json

    from server.db import session
    from tests.api.test_models_and_attachments import StaticCatalog
    from tests.support.agent_double import FakeAgentGateway

    skill = service.create_skill(name="汇报", description="简洁汇报", body="先结论，再依据")
    selection = {
        "skills": [{"id": skill.id, "revision": skill.content_hash}],
        "excluded_skill_ids": ["excluded"],
        "auto_match_skills": False,
    }
    app = create_app(gateway=FakeAgentGateway(), model_catalog=StaticCatalog())
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            data={"model": "model-a", "message": "整理附件", "selection": json.dumps(selection)},
            files=[("files", ("notes.md", b"notes", "text/markdown"))],
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["task_id"]
        reply = client.post(
            f"/api/tasks/{task_id}/messages",
            data={"message": "继续", "selection": json.dumps(selection)},
        )
        assert reply.status_code == 202, reply.text
        with session() as conn:
            rows = conn.execute(
                "SELECT input FROM agent_runs WHERE task_id=?", (task_id,)
            ).fetchall()
        assert len(rows) == 2
        for row in rows:
            payload = json.loads(row[0])
            assert payload["skill_refs"] == selection["skills"]
            assert payload["excluded_skill_ids"] == ["excluded"]
            assert payload["auto_match_skills"] is False
        assert len(json.loads(rows[0][0])["attachment_ids"]) == 1
        invalid = client.post(
            f"/api/tasks/{task_id}/messages", data={"message": "继续", "selection": "bad-json"}
        )
        assert invalid.status_code == 422
