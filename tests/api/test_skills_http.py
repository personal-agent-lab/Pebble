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
