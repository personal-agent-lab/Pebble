"""iCloud Calendar：直连创建、冲突处理、协议内容与工具范围。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar, Event

from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, build_tools, exposed_tools
from server.approval.service import ConfirmationService
from server.config import get_settings
from server.db import init_db, session
from server.errors import DraftValidationError, VersionConflictError
from server.main import create_app
from server.sessions import runs
from server.sessions.service import SessionStore
from server.tools.calendar.client import CalDAVCalendarClient, event_record
from server.tools.calendar.service import CalendarEventStore, stored_event
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
    def __init__(self, conflicts=None):
        self.conflicts = conflicts or []
        self.windows = []

    def list_events(self, time_min, time_max, calendar_id, max_results):
        return {"events": []}

    def get_event(self, event_id):
        return {"event_id": event_id}

    def check_conflicts(self, start, end, calendar_id):
        self.windows.append((start, end))
        return {"busy": [], "conflicts": self.conflicts}

    def conflict_window(self, fields):
        return CalDAVCalendarClient.conflict_window(fields)


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


def test_saved_events_are_immutable_and_validated(calendar_settings):
    init_db()
    task = SessionStore().create_task("安排会议")
    store = CalendarEventStore()
    # 每次创建都新建操作：直连创建没有待确认版本，不存在复用历史操作的路径。
    first = store.save_event(task["task_id"], **FIELDS)
    second = store.save_event(task["task_id"], **FIELDS)
    assert first["version"] == second["version"] == 1
    assert first["operation_id"] != second["operation_id"]
    with session() as conn:
        stored = stored_event(conn, first["operation_id"])
    assert stored["summary"] == FIELDS["summary"]
    # 账号与日历地址是执行期内部字段，不回流给模型。
    assert stored["account"] == "owner@example.com"
    with pytest.raises(DraftValidationError):
        store.save_event(task["task_id"], **{**FIELDS, "start": "2026-10-12T10:00:00"})


def test_all_day_event_uses_exclusive_end(calendar_settings):
    init_db()
    task = SessionStore().create_task("全天安排")
    saved = CalendarEventStore().save_event(
        task["task_id"],
        **{**FIELDS, "start": "2026-10-12", "end": "2026-10-13", "all_day": True},
    )
    with session() as conn:
        assert stored_event(conn, saved["operation_id"])["all_day"] is True
    with pytest.raises(DraftValidationError):
        CalendarEventStore().save_event(
            task["task_id"],
            **{**FIELDS, "start": "2026-10-13", "end": "2026-10-12", "all_day": True},
        )


def test_confirmation_creates_exact_version_once_and_verifies_unknown(calendar_settings):
    init_db()
    task = SessionStore().create_task("安排会议")
    saved = CalendarEventStore().save_event(task["task_id"], **FIELDS)
    calls = []

    def create(**values):
        calls.append(values)
        return {"status": "created", "event_id": "event-1"}

    service = ConfirmationService(None, create_event=create)
    with pytest.raises(VersionConflictError):
        service.accept_confirmation(task["task_id"], saved["operation_id"], 2)
    with ThreadPoolExecutor(2) as pool:
        list(
            pool.map(
                lambda _: service.accept_confirmation(task["task_id"], saved["operation_id"], 1),
                range(2),
            )
        )
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: service.execute_accepted(saved["operation_id"]), range(2)))
    assert len(calls) == 1
    assert calls[0]["fields"]["description"] == FIELDS["description"]
    assert service.get_execution(saved["operation_id"])["result"] == {
        "status": "created",
        "event_id": "event-1",
    }

    other_task = SessionStore().create_task("结果待核实")
    other = CalendarEventStore().save_event(
        other_task["task_id"], **{**FIELDS, "summary": "另一会议"}
    )
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
    internal = {**FIELDS, "account": client.account, "calendar_url": client.calendar_url}
    result = client.create_event(operation_id="op-1", version=1, fields=internal)
    assert result == {"status": "created", "event_id": "pebble-op-1@local"}
    assert len(dav.calls) == 1
    assert dav.calls[0][2]["If-None-Match"] == "*"
    assert "ATTENDEE" not in dav.data and "ORGANIZER" not in dav.data


def test_stale_calendar_config_refuses_creation(calendar_settings, monkeypatch):
    client = CalDAVCalendarClient(calendar_settings)
    dav = DAV()
    monkeypatch.setattr(client, "_connection", lambda: dav)
    fields = {**FIELDS, "account": "other@example.com", "calendar_url": client.calendar_url}
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


def _recorder(calls):
    """替身创建函数：记下每次调用的参数并报告创建成功。"""

    def create(**values):
        calls.append(values)
        return {"status": "created", "event_id": "pebble-op-1@local"}

    return create


def _deps(calendar=None, confirmations=None):
    return ToolDeps(
        drafts=MailDraftStore(),
        tasks=SessionStore(),
        gmail=MockGmailClient(),
        calendar=calendar if calendar is not None else Reader(),
        calendar_events=CalendarEventStore(),
        confirmations=confirmations,
    )


def _bound(deps, name):
    return next(tool for tool in build_tools(deps) if tool.name == name)


def test_calendar_tools_follow_turn_visibility(calendar_settings):
    init_db()
    confirmations = ConfirmationService(None, create_event=lambda **_: {})
    tools = build_tools(_deps(confirmations=confirmations))
    visible = {
        kind: {tool.name for tool in exposed_tools(tools, allowed=ALLOWED_EFFECTS[kind])}
        for kind in TurnKind
    }
    assert {
        "calendar_list_events",
        "calendar_get_event",
        "calendar_check_conflicts",
    } <= visible[TurnKind.NEW_MAIL]
    # 直连创建只出现在用户亲自发起的轮次：触发轮与结果回传轮的输入都来自系统。
    assert "calendar_create_event" in visible[TurnKind.MESSAGE]
    assert "calendar_create_event" not in visible[TurnKind.NEW_MAIL]
    assert "calendar_create_event" not in visible[TurnKind.EXECUTION_RESULT]
    # 确认服务未接入时该工具不参与装配，不伪造创建能力。
    assert "calendar_create_event" not in {tool.name for tool in build_tools(_deps())}


def test_direct_creation_writes_once_and_records_no_timeline_item(calendar_settings):
    init_db()
    task = SessionStore().create_task("安排会议")
    calls = []
    confirmations = ConfirmationService(None, create_event=_recorder(calls))
    with TestClient(create_app(confirmations=confirmations)) as client:
        result = _bound(_deps(confirmations=confirmations), "calendar_create_event")(
            task_id=task["task_id"], **FIELDS
        )
        assert result["status"] == "created" and result["version"] == 1
        assert len(calls) == 1
        assert confirmations.get_execution(result["operation_id"])["status"] == "created"
        with session() as conn:
            kinds = [row["kind"] for row in runs.runs(conn, task["task_id"])]
        assert runs.KIND_EXECUTION_RESULT not in kinds
        # 创建结果只留在对话文字与数据库记录里，时间线不再有日程卡片。
        assert client.get(f"/api/tasks/{task['task_id']}/timeline").json()["items"] == []
        operations = client.get(f"/api/tasks/{task['task_id']}/operations").json()
        assert [(item["type"], item["status"]) for item in operations] == [("calendar", "created")]


def test_conflict_returns_clashes_without_recording(calendar_settings):
    init_db()
    task = SessionStore().create_task("安排会议")
    calls = []
    confirmations = ConfirmationService(None, create_event=_recorder(calls))
    reader = Reader(conflicts=[{"event_id": "existing", "summary": "已有会议"}])
    result = _bound(_deps(calendar=reader, confirmations=confirmations), "calendar_create_event")(
        task_id=task["task_id"], **FIELDS
    )
    assert result == {
        "status": "conflict",
        "conflicts": [{"event_id": "existing", "summary": "已有会议"}],
    }
    assert calls == []
    assert reader.windows == [(FIELDS["start"], FIELDS["end"])]
    with TestClient(create_app()) as client:
        assert client.get(f"/api/tasks/{task['task_id']}/operations").json() == []


def test_conflict_override_creates_after_user_insists(calendar_settings):
    init_db()
    task = SessionStore().create_task("安排会议")
    calls = []
    confirmations = ConfirmationService(None, create_event=_recorder(calls))
    reader = Reader(conflicts=[{"event_id": "existing", "summary": "已有会议"}])
    result = _bound(_deps(calendar=reader, confirmations=confirmations), "calendar_create_event")(
        task_id=task["task_id"], overwrite_conflicts=True, **FIELDS
    )
    assert result["status"] == "created"
    assert len(calls) == 1
    with session() as conn:
        kinds = [row["kind"] for row in runs.runs(conn, task["task_id"])]
    assert runs.KIND_EXECUTION_RESULT not in kinds


def test_conflict_window_expands_all_day_to_utc_midnight():
    assert CalDAVCalendarClient.conflict_window(FIELDS) == (FIELDS["start"], FIELDS["end"])
    assert CalDAVCalendarClient.conflict_window(
        {**FIELDS, "start": "2026-10-12", "end": "2026-10-13", "all_day": True}
    ) == ("2026-10-12T00:00:00+00:00", "2026-10-13T00:00:00+00:00")
