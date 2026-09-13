"""真实 SQLite/HTTP/SDK 装配；只有外部日历和模型响应使用替身。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from caldav.lib.error import AuthorizationError
from caldav.response import DAVResponse
from fastapi.testclient import TestClient
from qodercn_agent_sdk import ResultMessage, SystemMessage

from server.agent import sdk_client
from server.approval.service import ConfirmationService
from server.config import Settings, get_settings
from server.db import init_db, session
from server.errors import NotEditableError, NotFoundError, VersionConflictError
from server.main import create_app
from server.sessions.service import SessionStore
from server.tools.calendar import client, tools
from server.tools.calendar.service import CalendarDraftStore, draft, validate
from server.tools.gmail.service import DraftValidationError

FIELDS = dict(
    title="项目会议",
    start="2026-10-12T10:00:00+10:30",
    end="2026-10-12T11:00:00+10:30",
    timezone="Australia/Adelaide",
    location="会议室",
    description="中文备注\n不发送邀请",
)


@pytest.fixture
def calendar(settings, monkeypatch):
    monkeypatch.setenv("PEBBLE_ICLOUD_ACCOUNT", "test@example.com")
    monkeypatch.setenv("PEBBLE_ICLOUD_PASSWORD_PATH", str(settings.data_dir / "password"))
    monkeypatch.setenv(
        "PEBBLE_ICLOUD_CALENDAR_URL", "https://p01-caldav.icloud.com/123/calendars/test/"
    )
    get_settings.cache_clear()
    init_db()
    task = SessionStore().create_task("新建日程")
    store = CalendarDraftStore()
    saved = store.prepare(task["task_id"], "request-1", **FIELDS)
    yield task, store, saved
    get_settings.cache_clear()


def internal(saved):
    with session() as conn:
        return draft(conn, saved["operation_id"])


class DAV:
    def __init__(self, status=201, error=None):
        self.status = status
        self.error = error
        self.calls = []
        self.data = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def put(self, url, body, headers):
        self.calls.append((url, body, headers))
        self.data = body
        if self.error:
            raise self.error
        return DAVResponse.from_bytes(b"", self.status)

    def request(self, url):
        return DAVResponse.from_bytes(self.data.encode(), 200)


def test_preview_versions_concurrency_and_confirmed_content(calendar):
    task, store, saved = calendar
    oid = saved["operation_id"]
    assert "account" not in saved
    assert store.prepare(task["task_id"], "request-1", **FIELDS)["operation_id"] == oid
    v2 = store.update(oid, 1, **{**FIELDS, "description": "最终版本"})
    assert CalendarDraftStore().get(oid)["version"] == 2
    assert store.get(oid, 1)["description"] == FIELDS["description"]
    calls = []

    def create(**values):
        calls.append(values)
        return {
            "status": "created",
            "uid": values["draft"]["uid"],
            "resource_url": client.resource_url(values["draft"]),
        }

    confirmations = ConfirmationService(None, create_event=create)
    assert not calls
    with pytest.raises(VersionConflictError):
        confirmations.accept_confirmation(task["task_id"], oid, 1)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: confirmations.confirm_reply(task["task_id"], oid, 2), range(2))
        )
    assert len(calls) == 1
    assert calls[0]["version"] == 2
    assert calls[0]["draft"]["description"] == v2["description"]
    assert all(r["status"] in {"creating", "created"} for r in results)
    assert confirmations.get_execution(oid)["status"] == "created"
    with pytest.raises(NotEditableError):
        store.update(oid, 2, **FIELDS)
    other = SessionStore().create_task("其他任务")
    with pytest.raises(NotFoundError):
        confirmations.accept_confirmation(other["task_id"], oid, 2)
    with pytest.raises(NotFoundError):
        tools.read_event_draft(other["task_id"], oid)


@pytest.mark.parametrize(
    "change",
    [
        {"title": " "},
        {"end": ""},
        {"end": "2026-10-12"},
        {"end": FIELDS["start"]},
        {"timezone": "invalid"},
        {"start": "2026-10-12T10:00:00+09:30"},
        {"start": "2026-10-04T02:30:00", "end": "2026-10-04T04:30:00"},
        {"start": "2026-04-05T02:30:00", "end": "2026-04-05T04:30:00"},
    ],
)
def test_reject_ambiguous_or_missing_fields(change):
    with pytest.raises(DraftValidationError):
        validate({**FIELDS, **change})


def test_explicit_dst_fold():
    assert validate(
        {**FIELDS, "start": "2026-04-05T02:30:00+10:30", "end": "2026-04-05T02:30:00+09:30"}
    )


def test_caldav_conditional_put_and_escaped_content(calendar, monkeypatch):
    _, _, saved = calendar
    dav = DAV()
    monkeypatch.setattr(client, "connection", lambda _: dav)
    values = internal(saved)
    values["description"] = "换行\nATTENDEE:mailto:bad@example.com\n逗号,分号;反斜线\\"
    result = client.create_event(operation_id=saved["operation_id"], version=1, draft=values)
    assert result["status"] == "created"
    assert len(dav.calls) == 1
    assert dav.calls[0][2]["If-None-Match"] == "*"
    assert client.matches(dav.data, values)
    assert not client.matches(dav.data, {**values, "title": "不同内容"})


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (403, None, "failed"),
        (500, None, "unknown"),
        (201, TimeoutError("secret"), "unknown"),
        (401, AuthorizationError("secret"), "failed"),
    ],
)
def test_provider_failures(calendar, monkeypatch, status, error, expected):
    _, _, saved = calendar
    dav = DAV(status, error)
    monkeypatch.setattr(client, "connection", lambda _: dav)
    result = client.create_event(
        operation_id=saved["operation_id"], version=1, draft=internal(saved)
    )
    assert result["status"] == expected
    assert "secret" not in json.dumps(result)
    assert len(dav.calls) == 1


def test_auth_failure_after_put_is_unknown(calendar, monkeypatch):
    _, _, saved = calendar
    dav = DAV()

    def read(url):
        raise AuthorizationError("secret")

    dav.request = read
    monkeypatch.setattr(client, "connection", lambda _: dav)
    assert (
        client.create_event(operation_id=saved["operation_id"], version=1, draft=internal(saved))[
            "status"
        ]
        == "unknown"
    )


def test_recovery_and_read_only_verification(calendar, monkeypatch):
    task, _, saved = calendar
    oid = saved["operation_id"]
    calls = []
    confirmations = ConfirmationService(None, create_event=lambda **kw: calls.append(kw))
    confirmations.accept_confirmation(task["task_id"], oid, 1)
    assert confirmations.recover_interrupted_executions() == [oid]
    confirmations.execute_accepted(oid)
    assert not calls
    assert confirmations.get_execution(oid)["status"] == "unknown"
    dav = DAV()
    monkeypatch.setattr(client, "connection", lambda _: dav)
    assert confirmations.verify_calendar(oid)["status"] == "unknown"
    dav.data = client.event_data(internal(saved))
    assert confirmations.verify_calendar(oid)["status"] == "created"
    assert not dav.calls
    assert confirmations.accept_confirmation(task["task_id"], oid, 1)["status"] == "created"
    with session() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE kind='execution_result'"
            ).fetchone()[0]
            == 2
        )


def test_sdk_to_http_calendar_chain(calendar, monkeypatch):
    task, store, saved = calendar
    tid, oid = task["task_id"], saved["operation_id"]
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=get_settings().data_dir, QODERCN_PERSONAL_ACCESS_TOKEN="test", _env_file=None
        ),
    )
    handlers = {}
    original = sdk_client.tool

    def register(name, description, schema):
        assert "task_id" not in schema["properties"]

        def decorate(handler):
            handlers[name] = handler
            return original(name, description, schema)(handler)

        return decorate

    monkeypatch.setattr(sdk_client, "tool", register)
    observed = []

    class SDK:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, message):
            self.message = message

        async def receive_response(self):
            yield SystemMessage("init", {"session_id": "calendar-session"})
            if self.message == "修改日程":
                current = await handlers["calendar_read_event_draft"]({"operation_id": oid})
                assert not current["isError"]
                result = await handlers["calendar_update_event_draft"](
                    {
                        "operation_id": oid,
                        "expected_version": 1,
                        **{**FIELDS, "title": "Agent 修改"},
                    }
                )
                assert not result["isError"]
            else:
                observed.append(self.options.system_prompt)
            yield ResultMessage("success", 1, 1, False, 1, "calendar-session")

    monkeypatch.setattr(sdk_client, "QoderSDKClient", SDK)
    dav = DAV()
    monkeypatch.setattr(client, "connection", lambda _: dav)
    app = create_app(gateway=sdk_client.QoderGateway(), create_event=client.create_event)
    with TestClient(app) as http:

        def wait_done(kind="message"):
            for _ in range(300):
                run = http.get(f"/api/tasks/{tid}").json()["latest_run"]
                if run and run["status"] == "done" and run["kind"] == kind:
                    return
                assert not run or run["status"] != "error", run
                time.sleep(0.01)
            pytest.fail("Agent 未结束")

        assert (
            http.post(f"/api/tasks/{tid}/messages", json={"message": "修改日程"}).status_code == 202
        )
        wait_done()
        assert store.get(oid)["title"] == "Agent 修改"
        assert not dav.calls
        response = http.patch(
            f"/api/operations/{oid}/calendar-draft",
            json={"expected_version": 2, **{**FIELDS, "title": "网页最终内容"}},
        )
        assert response.status_code == 200
        assert response.json()["version"] == 3
        assert (
            http.get(f"/api/operations/{oid}/calendar-draft?version=1").json()["title"]
            == FIELDS["title"]
        )
        assert (
            http.post(
                f"/api/tasks/{tid}/confirmations", json={"operation_id": oid, "version": 2}
            ).status_code
            == 409
        )
        assert not dav.calls
        assert (
            http.post(
                f"/api/tasks/{tid}/confirmations", json={"operation_id": oid, "version": 3}
            ).status_code
            == 202
        )
        wait_done("execution_result")
        assert len(dav.calls) == 1
        assert client.matches(dav.data, internal(saved))
        assert any('"status": "created"' in p for p in observed)
        assert (
            http.post(
                f"/api/tasks/{tid}/confirmations", json={"operation_id": oid, "version": 3}
            ).json()["status"]
            == "created"
        )
        assert len(dav.calls) == 1
        # External creation is not an SDK tool; no calendar tools in new-mail-only turns.
        options = sdk_client.build_options(tid, allow_drafts=False)
        assert not any("calendar" in name for name in options.allowed_tools)
        assert "calendar_create_event" not in handlers


def test_configuration_change_fails_without_network(calendar, monkeypatch):
    _, _, saved = calendar
    monkeypatch.setenv("PEBBLE_ICLOUD_ACCOUNT", "another@example.com")
    get_settings.cache_clear()
    result = client.create_event(
        operation_id=saved["operation_id"], version=1, draft=internal(saved)
    )
    assert result["status"] == "failed"


def test_schema3_migration_preserves_mail_and_rolls_back(settings, monkeypatch):
    from server import db
    from server.db import write

    with session() as conn, write(conn):
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES (3)")
        for migration in (db.SCHEMA_V1, db.SCHEMA_V2, db.SCHEMA_V3):
            for statement in migration:
                conn.execute(statement)
        conn.execute("INSERT INTO tasks VALUES ('t', 'mail', NULL, 'now')")
        conn.execute(
            "INSERT INTO operations VALUES ('o', 'mail_reply', 't', 1, 'sent', 'now', 'now')"
        )
        conn.execute("INSERT INTO task_operations VALUES ('t', 'o')")
        conn.execute("INSERT INTO mail_reply_drafts VALUES ('o', 'msg', 'thread')")
        conn.execute(
            "INSERT INTO mail_reply_versions VALUES ('o', 1, '[]', 'subject', 'body', 'now')"
        )
        conn.execute(
            "INSERT INTO approval_executions "
            "(operation_id,task_id,version,confirmed_at,message_id,completed_at) "
            "VALUES ('o','t',1,'now','sent-id','now')"
        )
    migration = db.SCHEMA_V4
    monkeypatch.setitem(db.SCHEMA_MIGRATIONS, 4, (*migration, "INVALID SQL"))
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        init_db()
    with session() as conn:
        assert db.schema_version(conn) == 3
        assert conn.execute("SELECT status FROM operations").fetchone()[0] == "sent"
    monkeypatch.setitem(db.SCHEMA_MIGRATIONS, 4, migration)
    assert init_db() == 4
    assert init_db() == 4
    with session() as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("SELECT body FROM mail_reply_versions").fetchone()[0] == "body"
    assert ConfirmationService(None).get_execution("o")["result"] == {
        "status": "sent",
        "message_id": "sent-id",
    }


def test_caldav_resource_collision_never_overwrites(calendar, monkeypatch):
    _, _, saved = calendar
    dav = DAV(412)
    dav.request = lambda _: DAVResponse.from_bytes(b"not the expected event", 200)
    monkeypatch.setattr(client, "connection", lambda _: dav)
    assert (
        client.create_event(operation_id=saved["operation_id"], version=1, draft=internal(saved))[
            "status"
        ]
        == "unknown"
    )
    assert len(dav.calls) == 1


def test_calendar_http_validation(calendar):
    _, _, saved = calendar
    with TestClient(create_app()) as http:
        url = f"/api/operations/{saved['operation_id']}/calendar-draft"
        assert http.patch(url, json={"expected_version": 1, **FIELDS, "end": ""}).status_code == 422
        assert (
            http.patch(
                url, json={"expected_version": 1, **FIELDS, "attendees": ["bad@example.com"]}
            ).status_code
            == 422
        )
        assert http.get(url).json()["version"] == 1
