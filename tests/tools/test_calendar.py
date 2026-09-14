"""iCloud Calendar：预览、确认、协议内容与工具范围。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar, Event

from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, build_tools, exposed_tools
from server.approval.service import ConfirmationService
from server.config import get_settings
from server.db import init_db, session, write
from server.errors import DraftValidationError, VersionConflictError
from server.main import create_app
from server.sessions import runs, timeline
from server.sessions.service import SessionStore, timestamp
from server.tools.calendar.client import CalDAVCalendarClient, event_record
from server.tools.calendar.service import CalendarPreviewStore
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient

FIELDS = {
    "summary": "项目会议",
    "start": "2026-10-12T10:00:00+08:00",
    "end": "2026-10-12T11:00:00+08:00",
    "all_day": False,
    "location": "会议室",
    "description": "讨论项目进度",
    "calendar_id": "primary",
}


class Reader:
    def list_events(self, time_min, time_max, calendar_id, max_results):
        return {"events": []}

    def get_event(self, event_id):
        return {"event_id": event_id}

    def check_conflicts(self, start, end, calendar_id):
        return {"busy": [], "conflicts": []}


@pytest.fixture
def calendar_settings(settings, monkeypatch):
    password = settings.data_dir / "icloud-password"
    password.write_text("app-password")
    monkeypatch.setenv("PEBBLE_ICLOUD_ACCOUNT", "owner@example.com")
    monkeypatch.setenv("PEBBLE_ICLOUD_PASSWORD_PATH", str(password))
    monkeypatch.setenv(
        "PEBBLE_ICLOUD_CALENDAR_URL", "https://p01-caldav.icloud.com/123/calendars/main/"
    )
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_preview_versions_reuse_and_validation(calendar_settings):
    init_db()
    task = SessionStore().create_task("安排会议")
    store = CalendarPreviewStore()
    first = store.save_preview(task["task_id"], **FIELDS)
    assert store.save_preview(task["task_id"], **FIELDS) == first
    second = store.update_preview(
        first["operation_id"], 1, **{**FIELDS, "summary": "项目会议（最终）"}
    )
    assert second["version"] == 2
    assert store.get_preview(first["operation_id"], 1)["summary"] == "项目会议"
    assert store.get_preview(first["operation_id"])["summary"] == "项目会议（最终）"
    assert "account" not in store.get_preview(first["operation_id"])
    with pytest.raises(VersionConflictError):
        store.update_preview(first["operation_id"], 1, **FIELDS)
    with pytest.raises(DraftValidationError):
        store.save_preview(task["task_id"], **{**FIELDS, "start": "2026-10-12T10:00:00"})


def test_all_day_preview_uses_exclusive_end(calendar_settings):
    init_db()
    task = SessionStore().create_task("全天安排")
    saved = CalendarPreviewStore().save_preview(
        task["task_id"],
        **{**FIELDS, "start": "2026-10-12", "end": "2026-10-13", "all_day": True},
    )
    assert CalendarPreviewStore().get_preview(saved["operation_id"])["all_day"] is True
    with pytest.raises(DraftValidationError):
        CalendarPreviewStore().save_preview(
            task["task_id"],
            **{**FIELDS, "start": "2026-10-13", "end": "2026-10-12", "all_day": True},
        )


def test_confirmation_creates_exact_version_once_and_verifies_unknown(calendar_settings):
    init_db()
    task = SessionStore().create_task("安排会议")
    store = CalendarPreviewStore()
    saved = store.save_preview(task["task_id"], **FIELDS)
    final = store.update_preview(saved["operation_id"], 1, **{**FIELDS, "description": "最终内容"})
    calls = []

    def create(**values):
        calls.append(values)
        return {"status": "created", "event_id": "event-1"}

    service = ConfirmationService(None, create_event=create)
    with pytest.raises(VersionConflictError):
        service.accept_confirmation(task["task_id"], saved["operation_id"], 1)
    with ThreadPoolExecutor(2) as pool:
        list(
            pool.map(
                lambda _: service.accept_confirmation(
                    task["task_id"], saved["operation_id"], final["version"]
                ),
                range(2),
            )
        )
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: service.execute_accepted(saved["operation_id"]), range(2)))
    assert len(calls) == 1
    assert calls[0]["fields"]["description"] == "最终内容"
    assert service.get_execution(saved["operation_id"])["result"] == {
        "status": "created",
        "event_id": "event-1",
    }

    other_task = SessionStore().create_task("结果待核实")
    other = store.save_preview(other_task["task_id"], **{**FIELDS, "summary": "另一会议"})
    verifies = []
    uncertain = ConfirmationService(
        None,
        create_event=lambda **_: {"status": "unknown", "reason": "超时"},
        verify_event=lambda **values: (
            verifies.append(values) or {"status": "created", "event_id": "event-2"}
        ),
    )
    uncertain.accept_confirmation(other_task["task_id"], other["operation_id"], 1)
    uncertain.execute_accepted(other["operation_id"])
    assert uncertain.verify_pending(other["operation_id"])["status"] == "created"
    assert len(verifies) == 1


def _ics(*, status="CONFIRMED", start="20261012T020000Z", end="20261012T030000Z"):
    calendar = Calendar()
    event = Event()
    event.add("uid", "uid-1")
    event.add("summary", "已有会议")
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("dtstamp", "20261001T000000Z")
    event.add("status", status)
    calendar.add_component(event)
    return calendar.to_ical().decode()


def test_event_normalization_includes_read_only_attendees():
    calendar = Calendar()
    event = Event()
    event.add("uid", "uid-1")
    event.add("summary", "外部会议")
    event.add("dtstart", datetime(2026, 10, 12, 2, tzinfo=UTC))
    event.add("dtend", datetime(2026, 10, 12, 3, tzinfo=UTC))
    event.add("dtstamp", datetime(2026, 10, 1, tzinfo=UTC))
    event.add("attendee", "mailto:guest@example.com", parameters={"PARTSTAT": "ACCEPTED"})
    calendar.add_component(event)
    record = event_record(SimpleNamespace(data=calendar.to_ical().decode()), description=True)
    assert record["event_id"] == "uid-1"
    assert record["attendees"] == [{"email": "guest@example.com", "response_status": "accepted"}]


class Response:
    def __init__(self, status, raw=""):
        self.status, self.raw = status, raw


class DAV:
    def __init__(self):
        self.calls = []
        self.data = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def put(self, url, body, headers):
        self.calls.append((url, body, headers))
        self.data = body
        return Response(201)

    def request(self, url):
        return Response(200, self.data)


def test_caldav_creation_is_conditional_exact_and_has_no_invites(calendar_settings, monkeypatch):
    client = CalDAVCalendarClient(calendar_settings)
    dav = DAV()
    monkeypatch.setattr(client, "_connection", lambda: dav)
    monkeypatch.setattr(client, "check_conflicts", lambda *args: {"busy": [], "conflicts": []})
    internal = {**FIELDS, "account": client.account, "calendar_url": client.calendar_url}
    result = client.create_event(operation_id="op-1", version=1, fields=internal)
    assert result == {"status": "created", "event_id": "pebble-op-1@local"}
    assert len(dav.calls) == 1
    assert dav.calls[0][2]["If-None-Match"] == "*"
    assert "ATTENDEE" not in dav.data and "ORGANIZER" not in dav.data


def test_conflict_blocks_creation_before_put(calendar_settings, monkeypatch):
    client = CalDAVCalendarClient(calendar_settings)
    dav = DAV()
    monkeypatch.setattr(client, "_connection", lambda: dav)
    monkeypatch.setattr(
        client,
        "check_conflicts",
        lambda *args: {"busy": [], "conflicts": [{"event_id": "existing"}]},
    )
    fields = {**FIELDS, "account": client.account, "calendar_url": client.calendar_url}
    assert client.create_event(operation_id="op-1", version=1, fields=fields)["status"] == "failed"
    assert dav.calls == []


def test_conflicts_expand_and_merge_timed_and_all_day(calendar_settings, monkeypatch):
    client = CalDAVCalendarClient(calendar_settings)

    def resource(uid, start, end, status="CONFIRMED"):
        calendar = Calendar()
        event = Event()
        event.add("uid", uid)
        event.add("summary", uid)
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime(2026, 10, 1, tzinfo=UTC))
        event.add("status", status)
        calendar.add_component(event)
        return SimpleNamespace(data=calendar.to_ical().decode())

    resources = [
        resource("all-day", date(2026, 10, 12), date(2026, 10, 13)),
        resource(
            "timed",
            datetime(2026, 10, 12, 2, tzinfo=UTC),
            datetime(2026, 10, 12, 3, tzinfo=UTC),
        ),
        resource(
            "cancelled",
            datetime(2026, 10, 12, 4, tzinfo=UTC),
            datetime(2026, 10, 12, 5, tzinfo=UTC),
            "CANCELLED",
        ),
    ]

    class Search:
        def search(self, **kwargs):
            assert kwargs["expand"] is True
            return resources

    monkeypatch.setattr(client, "_connection", lambda: DAV())
    monkeypatch.setattr(client, "_calendar", lambda _: Search())
    result = client.check_conflicts("2026-10-12T00:00:00+00:00", "2026-10-13T00:00:00+00:00")
    assert [event["event_id"] for event in result["conflicts"]] == ["all-day", "timed"]
    assert result["busy"] == [{"start": "2026-10-12", "end": "2026-10-13"}]


def test_calendar_tools_follow_turn_visibility(calendar_settings):
    init_db()
    deps = ToolDeps(
        drafts=MailDraftStore(),
        tasks=SessionStore(),
        gmail=MockGmailClient(),
        calendar=Reader(),
        calendar_previews=CalendarPreviewStore(),
    )
    tools = build_tools(deps)
    readonly = {
        tool.name for tool in exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.NEW_MAIL])
    }
    message = {
        tool.name for tool in exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
    }
    assert {"calendar_list_events", "calendar_get_event", "calendar_check_conflicts"} <= readonly
    assert "calendar_prepare_event" not in readonly
    assert {"calendar_prepare_event", "calendar_update_preview"} <= message


def test_calendar_preview_http_and_timeline(calendar_settings):
    with TestClient(create_app()) as client:
        task_id = client.post("/api/tasks", json={"goal": "安排会议"}).json()["task_id"]
        saved = CalendarPreviewStore().save_preview(task_id, **FIELDS)
        with session() as conn, write(conn):
            run_id = "calendar-run"
            runs.insert(
                conn, run_id, task_id, runs.KIND_MESSAGE, {"message": "安排会议"}, None, timestamp()
            )
            timeline.ensure_calendar_preview(conn, task_id, run_id, saved["operation_id"])
        item = client.get(f"/api/tasks/{task_id}/timeline").json()["items"][0]
        assert item["kind"] == "calendar_preview"
        assert item["preview"]["summary"] == FIELDS["summary"]
        edited = client.patch(
            f"/api/operations/{saved['operation_id']}/calendar-preview",
            json={"expected_version": 1, **{**FIELDS, "summary": "网页最终内容"}},
        )
        assert edited.status_code == 200 and edited.json()["version"] == 2
