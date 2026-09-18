"""访问控制：只接受名单内的 Tailscale 身份，写请求另要求来源是 Pebble 页面。"""

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from server.api.access import AccessGuard, AccessPolicy
from server.main import create_app
from tests.support.gmail_double import send

OWNER = "owner@example.com"
ORIGIN = "https://pebble.example.ts.net"
POLICY = AccessPolicy(frozenset({OWNER}), ORIGIN + "/")
OWNER_HEADERS = {"Tailscale-User-Login": OWNER}


@pytest.fixture
def app(settings):
    return create_app(send_message=send, access=POLICY)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def pending_draft(app) -> dict:
    task_id = app.state.tasks.create_task("回复邀请")["task_id"]
    return app.state.drafts.save_reply_draft(
        task_id, "mail-1", "thread-1", to=["a@example.com"], subject="回复", body="好的"
    ) | {"task_id": task_id}


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/tasks",
        "/api/kb/documents",
        "/api/memory",
        "/api/tasks/any/events",
        "/api/tasks/any/attachments/any",
    ],
)
def test_requests_without_identity_are_rejected(client, path):
    response = client.get(path)

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_accounts_outside_the_allow_list_are_rejected(client):
    response = client.get("/api/tasks", headers={"Tailscale-User-Login": "guest@example.com"})

    assert response.status_code == 403


def test_allowed_account_matches_case_insensitively(client):
    response = client.get("/api/tasks", headers={"Tailscale-User-Login": "Owner@Example.com"})

    assert response.status_code == 200


def test_page_requests_get_a_plain_explanation(client):
    response = client.get("/tasks")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/plain")
    assert "无权访问" in response.text


@pytest.mark.parametrize("origin", [None, "https://evil.example.com", "http://127.0.0.1:5173"])
def test_writes_from_other_origins_are_rejected(app, client, origin):
    draft = pending_draft(app)
    headers = OWNER_HEADERS | ({"Origin": origin} if origin else {})

    edited = client.patch(
        f"/api/operations/{draft['operation_id']}/draft",
        json={
            "expected_version": draft["version"],
            "to": ["x@example.com"],
            "subject": "改",
            "body": "改",
        },
        headers=headers,
    )
    confirmed = client.post(
        f"/api/tasks/{draft['task_id']}/confirmations",
        json={"operation_id": draft["operation_id"], "version": draft["version"]},
        headers=headers,
    )

    assert edited.status_code == confirmed.status_code == 403
    assert edited.json()["error"] == confirmed.json()["error"] == "bad_origin"
    current = app.state.drafts.get_draft(draft["operation_id"])
    assert current["version"] == draft["version"]
    assert current["status"] == "pending"


def test_writes_from_the_pebble_page_pass(app, client):
    draft = pending_draft(app)

    edited = client.patch(
        f"/api/operations/{draft['operation_id']}/draft",
        json={
            "expected_version": draft["version"],
            "to": ["x@example.com"],
            "subject": "改",
            "body": "改",
        },
        headers=OWNER_HEADERS | {"Origin": ORIGIN},
    )

    assert edited.status_code == 200
    assert app.state.drafts.get_draft(draft["operation_id"])["subject"] == "改"


def test_policy_requires_users_and_origin():
    with pytest.raises(ValueError, match="PEBBLE_ALLOWED_USERS"):
        AccessPolicy(frozenset(), ORIGIN)
    with pytest.raises(ValueError, match="PEBBLE_PUBLIC_ORIGIN"):
        AccessPolicy(frozenset({OWNER}), "")


def test_streaming_responses_pass_through_unbuffered():
    async def chunks():
        yield b"event: a\n\n"
        yield b"event: b\n\n"

    async def stream(request):
        return StreamingResponse(chunks(), media_type="text/event-stream")

    guarded = AccessGuard(Starlette(routes=[Route("/api/stream", stream)]), POLICY)

    with (
        TestClient(guarded) as client,
        client.stream("GET", "/api/stream", headers=OWNER_HEADERS) as response,
    ):
        received = list(response.iter_bytes())

    assert response.status_code == 200
    assert b"".join(received) == b"event: a\n\nevent: b\n\n"
