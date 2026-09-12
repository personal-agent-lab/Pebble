"""确认执行：真实落盘 SQLite + 显式发送替身，全部经实际服务接口操作。"""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from server import db
from server.approval import repository as approval_repo
from server.approval.service import ConfirmationService, recover_interrupted_executions
from server.db import SCHEMA_VERSION, init_db, session, write
from server.main import app
from server.sessions.errors import NotEditableError, NotFoundError, VersionConflictError
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore

FINAL = {
    "to": ["甲@example.com", "b@example.com", "c@example.com"],
    "subject": " 回复：活动邀请 ",
    "body": "你好，\n\n我参加。\n\n谢谢！\n",
}

_SENT = object()


def valid(**kwargs):
    return {"valid": True, "errors": []}


class Sender:
    """发送替身：记录每次调用的参数，可注入指定返回值、异常或调用前动作。"""

    def __init__(self, result=_SENT, before_call=None):
        self.result = {"status": "sent", "message_id": "sent-1"} if result is _SENT else result
        self.before_call = before_call
        self.calls: list[dict] = []

    def __call__(self, **fields):
        self.calls.append(fields)
        if self.before_call is not None:
            self.before_call()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def stores(settings):
    init_db()
    return SessionStore(), ReplyDraftStore(valid)


def prepare(stores, source="m1"):
    tasks, drafts = stores
    task = tasks.create_task("处理活动邀请")
    operation = drafts.save_reply_draft(task["task_id"], source, "thread-1", **FINAL)
    return task, operation


def run_python(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, *args], check=True, capture_output=True, text=True
    )


def test_confirm_sends_exact_confirmed_version(stores):
    task, operation = prepare(stores)
    drafts = stores[1]
    second = drafts.update_reply_draft(
        operation["operation_id"], 1, **{**FINAL, "subject": "回复：活动邀请"}
    )
    final = {
        "to": ["乙@example.com", "甲@example.com", "c@example.com"],
        "subject": "回复：活动邀请（确认）",
        "body": "  最终版：\n\n我参加。\n",
    }
    third = drafts.update_reply_draft(operation["operation_id"], second["version"], **final)
    assert third["version"] == 3

    sender = Sender()
    service = ConfirmationService(sender)
    response = service.confirm_reply(task["task_id"], operation["operation_id"], 3)

    assert sender.calls == [
        {
            "operation_id": operation["operation_id"],
            "version": 3,
            "source_message_id": "m1",
            "thread_id": "thread-1",
            "to": final["to"],
            "subject": final["subject"],
            "body": final["body"],
        }
    ]
    assert response["operation_id"] == operation["operation_id"]
    assert response["version"] == 3
    assert response["status"] == "sent"
    assert response["confirmation"]["task_id"] == task["task_id"]
    assert response["confirmation"]["version"] == 3
    assert response["confirmation"]["confirmed_at"]
    assert response["result"] == {"status": "sent", "message_id": "sent-1"}
    assert service.get_execution(operation["operation_id"]) == response

    assert drafts.get_reply_draft(operation["operation_id"], 1)["subject"] == FINAL["subject"]
    assert drafts.get_reply_draft(operation["operation_id"], 1)["to"] == FINAL["to"]
    assert drafts.get_reply_draft(operation["operation_id"], 2)["subject"] == "回复：活动邀请"


def test_confirm_rejects_missing_objects_and_stale_version(stores):
    task, operation = prepare(stores)
    drafts = stores[1]
    drafts.update_reply_draft(operation["operation_id"], 1, **FINAL)
    sender = Sender()
    service = ConfirmationService(sender)

    with pytest.raises(VersionConflictError) as error:
        service.confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert error.value.current_version == 2
    assert sender.calls == []
    assert service.get_execution(operation["operation_id"]) == {
        "operation_id": operation["operation_id"],
        "version": 2,
        "status": "pending",
        "confirmation": None,
        "result": None,
    }
    with pytest.raises(NotFoundError):
        service.confirm_reply(task["task_id"], "missing-operation", 2)
    with pytest.raises(NotFoundError):
        service.confirm_reply("missing-task", operation["operation_id"], 2)
    with pytest.raises(NotFoundError):
        service.get_execution("missing-operation")
    with pytest.raises(NotFoundError):
        service.get_agent_result("missing-operation")


def test_confirm_requires_pending_without_execution(stores):
    task, operation = prepare(stores)
    with session() as conn, write(conn):
        conn.execute("UPDATE operations SET status = 'sending'")
    sender = Sender()
    with pytest.raises(NotEditableError) as error:
        ConfirmationService(sender).confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert error.value.status == "sending"
    assert sender.calls == []


def test_concurrent_confirmation_single_execution(stores):
    task, operation = prepare(stores)
    started = Event()
    release = Event()

    def pause():
        started.set()
        assert release.wait(5)

    sender = Sender(before_call=pause)
    service = ConfirmationService(sender)
    other = Sender()

    with ThreadPoolExecutor(2) as pool:
        winner = pool.submit(service.confirm_reply, task["task_id"], operation["operation_id"], 1)
        assert started.wait(5)
        duplicate = ConfirmationService(other).confirm_reply(
            task["task_id"], operation["operation_id"], 1
        )
        release.set()
        sent = winner.result(timeout=5)

    assert other.calls == []
    assert duplicate["status"] == "sending"
    assert duplicate["result"] is None
    assert duplicate["confirmation"]["task_id"] == task["task_id"]
    assert sent["status"] == "sent"
    assert len(sender.calls) == 1
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approval_executions").fetchone()[0] == 1


def test_edit_and_confirm_race(stores):
    task, operation = prepare(stores)
    drafts = stores[1]
    sender = Sender()
    service = ConfirmationService(sender)
    edited = {**FINAL, "body": "编辑后的正文\n"}
    barrier = Barrier(2)

    def try_edit() -> str:
        barrier.wait(timeout=5)
        try:
            drafts.update_reply_draft(operation["operation_id"], 1, **edited)
            return "edited"
        except (NotEditableError, VersionConflictError) as error:
            return type(error).__name__

    def try_confirm() -> dict | str:
        barrier.wait(timeout=5)
        try:
            return service.confirm_reply(task["task_id"], operation["operation_id"], 1)
        except (NotEditableError, VersionConflictError) as error:
            return type(error).__name__

    with ThreadPoolExecutor(2) as pool:
        edit_future = pool.submit(try_edit)
        confirm_future = pool.submit(try_confirm)
        edit_result = edit_future.result(timeout=5)
        confirm_result = confirm_future.result(timeout=5)

    if sender.calls:
        assert isinstance(confirm_result, dict)
        assert confirm_result["status"] == "sent"
        assert sender.calls[0]["version"] == 1
        assert sender.calls[0]["body"] == FINAL["body"]
        assert edit_result == "NotEditableError"
        assert drafts.get_reply_draft(operation["operation_id"])["body"] == FINAL["body"]
    else:
        assert confirm_result == "VersionConflictError"
        assert edit_result == "edited"
        current = drafts.get_reply_draft(operation["operation_id"])
        assert (current["version"], current["body"]) == (2, edited["body"])


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            {"status": "sent", "message_id": "gmail-42"},
            {"status": "sent", "message_id": "gmail-42"},
        ),
        (
            {"status": "failed", "reason": "收件人被拒绝"},
            {"status": "failed", "reason": "收件人被拒绝"},
        ),
        (
            {"status": "unknown", "reason": "网关超时"},
            {"status": "unknown", "reason": "网关超时"},
        ),
    ],
)
def test_send_result_persists_for_new_process(stores, outcome, expected):
    task, operation = prepare(stores)
    stores[0].bind_sdk_session(task["task_id"], "sdk-1")
    sender = Sender(outcome)
    service = ConfirmationService(sender)

    response = service.confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert response["status"] == expected["status"]
    assert response["result"] == expected
    assert service.confirm_reply(task["task_id"], operation["operation_id"], 1) == response
    assert len(sender.calls) == 1

    reader = f"""
import json

from server.approval.service import ConfirmationService


def sender(**kwargs):
    raise AssertionError("读取不应调用发送函数")


service = ConfirmationService(sender)
print(json.dumps({{"execution": service.get_execution({operation["operation_id"]!r}),
                   "agent": service.get_agent_result({operation["operation_id"]!r})}}))
"""
    assert json.loads(run_python(reader).stdout) == {
        "execution": response,
        "agent": {
            "task_id": task["task_id"],
            "sdk_session_id": "sdk-1",
            "operation_id": operation["operation_id"],
            "version": 1,
            "result": expected,
        },
    }


@pytest.mark.parametrize(
    "returned",
    [
        RuntimeError("网络超时"),
        None,
        "sent",
        {"status": "sent"},
        {"status": "failed"},
        {"status": "finished", "message_id": "x"},
        {"status": "sent", "message_id": 42},
    ],
)
def test_unclear_send_result_saved_as_unknown(stores, returned):
    task, operation = prepare(stores)
    sender = Sender(returned)
    service = ConfirmationService(sender)

    response = service.confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert response["status"] == "unknown"
    assert response["result"]["status"] == "unknown"
    assert response["result"]["reason"]

    assert service.confirm_reply(task["task_id"], operation["operation_id"], 1) == response
    assert service.get_execution(operation["operation_id"]) == response
    assert len(sender.calls) == 1


def test_claim_rollback_leaves_no_trace(stores, monkeypatch):
    task, operation = prepare(stores)
    sender = Sender()
    real_insert = approval_repo.insert
    state = {"fail": True}

    def interrupted(*args, **kwargs):
        real_insert(*args, **kwargs)
        if state["fail"]:
            raise RuntimeError("提交前中断")

    monkeypatch.setattr(approval_repo, "insert", interrupted)
    service = ConfirmationService(sender)
    with pytest.raises(RuntimeError):
        service.confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert sender.calls == []
    assert stores[1].get_reply_draft(operation["operation_id"])["status"] == "pending"
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approval_executions").fetchone()[0] == 0

    state["fail"] = False
    assert service.confirm_reply(task["task_id"], operation["operation_id"], 1)["status"] == "sent"


def test_result_save_failure_blocks_resend(stores, monkeypatch):
    task, operation = prepare(stores)
    sender = Sender()
    real_complete = approval_repo.complete
    state = {"fail": True}

    def flaky(*args, **kwargs):
        if state["fail"]:
            raise sqlite3.OperationalError("模拟落盘失败")
        return real_complete(*args, **kwargs)

    monkeypatch.setattr(approval_repo, "complete", flaky)
    service = ConfirmationService(sender)
    with pytest.raises(sqlite3.OperationalError):
        service.confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert len(sender.calls) == 1
    interrupted = service.get_execution(operation["operation_id"])
    assert interrupted["status"] == "sending"
    assert interrupted["result"] is None
    assert interrupted["confirmation"]["version"] == 1

    state["fail"] = False
    assert service.confirm_reply(task["task_id"], operation["operation_id"], 1) == interrupted
    assert len(sender.calls) == 1

    assert service.recover_interrupted_executions() == [operation["operation_id"]]
    recovered = service.get_execution(operation["operation_id"])
    assert recovered["status"] == "unknown"
    assert recovered["result"]["status"] == "unknown"
    assert recovered["confirmation"] == interrupted["confirmation"]
    assert service.confirm_reply(task["task_id"], operation["operation_id"], 1) == recovered
    assert len(sender.calls) == 1


def test_process_exit_during_execution_recovers_as_unknown(stores):
    task, operation = prepare(stores)
    crash = """
import os
import sys

from server.approval.service import ConfirmationService


def sender(**kwargs):
    os._exit(3)


ConfirmationService(sender).confirm_reply(sys.argv[1], sys.argv[2], int(sys.argv[3]))
"""
    result = subprocess.run(
        [sys.executable, "-c", crash, task["task_id"], operation["operation_id"], "1"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3

    sender = Sender()
    service = ConfirmationService(sender)
    interrupted = service.get_execution(operation["operation_id"])
    assert interrupted["status"] == "sending"
    assert interrupted["result"] is None
    assert service.get_agent_result(operation["operation_id"]) is None

    assert recover_interrupted_executions() == [operation["operation_id"]]
    recovered = service.get_execution(operation["operation_id"])
    assert recovered["status"] == "unknown"
    assert recovered["result"]["status"] == "unknown"
    assert recovered["confirmation"] == interrupted["confirmation"]
    assert service.confirm_reply(task["task_id"], operation["operation_id"], 1) == recovered
    assert sender.calls == []


def test_startup_lifespan_recovers_interrupted_execution(stores):
    task, operation = prepare(stores)
    with session() as conn, write(conn):
        conn.execute(
            "INSERT INTO approval_executions (operation_id, task_id, version, confirmed_at) "
            "VALUES (?, ?, 1, '2026-09-12T00:00:00+00:00')",
            (operation["operation_id"], task["task_id"]),
        )
        conn.execute("UPDATE operations SET status = 'sending'")
    sender = Sender()

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    execution = ConfirmationService(sender).get_execution(operation["operation_id"])
    assert execution["status"] == "unknown"
    assert sender.calls == []


def test_delivery_task_is_first_confirmation_task(stores):
    tasks, drafts = stores
    first = tasks.create_task("处理活动邀请")
    second = tasks.create_task("再检查一次")
    operation = drafts.save_reply_draft(first["task_id"], "m1", "thread-1", **FINAL)
    reused = drafts.save_reply_draft(second["task_id"], "m1", "ignored", **FINAL)
    assert reused == operation
    tasks.bind_sdk_session(first["task_id"], "sdk-first")
    tasks.bind_sdk_session(second["task_id"], "sdk-second")

    sender = Sender({"status": "failed", "reason": "SMTP 拒绝"})
    service = ConfirmationService(sender)
    response = service.confirm_reply(second["task_id"], operation["operation_id"], 1)

    assert response["status"] == "failed"
    assert service.get_agent_result(operation["operation_id"]) == {
        "task_id": second["task_id"],
        "sdk_session_id": "sdk-second",
        "operation_id": operation["operation_id"],
        "version": 1,
        "result": {"status": "failed", "reason": "SMTP 拒绝"},
    }

    assert service.confirm_reply(first["task_id"], operation["operation_id"], 1) == response
    assert len(sender.calls) == 1
    agent = service.get_agent_result(operation["operation_id"])
    assert agent["task_id"] == second["task_id"]
    assert agent["sdk_session_id"] == "sdk-second"
    with session() as conn:
        row = conn.execute(
            "SELECT task_id FROM approval_executions WHERE operation_id = ?",
            (operation["operation_id"],),
        ).fetchone()
        assert row["task_id"] == second["task_id"]


def test_agent_result_needs_saved_result_and_session(stores):
    task, operation = prepare(stores)
    sender = Sender()
    service = ConfirmationService(sender)

    assert service.get_agent_result(operation["operation_id"]) is None
    response = service.confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert response["result"] is not None
    assert service.get_agent_result(operation["operation_id"]) is None

    stores[0].bind_sdk_session(task["task_id"], "sdk-late")
    assert service.get_agent_result(operation["operation_id"]) == {
        "task_id": task["task_id"],
        "sdk_session_id": "sdk-late",
        "operation_id": operation["operation_id"],
        "version": 1,
        "result": {"status": "sent", "message_id": "sent-1"},
    }


def test_upgrade_from_v1_preserves_records(settings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn, write(conn):
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES (1)")
        for statement in db.SCHEMA_V1:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO tasks VALUES ('t1', '旧任务', 'sdk-old', '2026-09-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO operations VALUES ('o1', 'mail_reply', 't1', 2, 'pending', "
            "'2026-09-01T00:00:00+00:00', '2026-09-01T01:00:00+00:00')"
        )
        conn.execute("INSERT INTO task_operations VALUES ('t1', 'o1')")
        conn.execute("INSERT INTO mail_reply_drafts VALUES ('o1', 'm-old', 'thread-old')")
        for version, body in ((1, "第一版"), (2, "第二版")):
            conn.execute(
                "INSERT INTO mail_reply_versions VALUES "
                "('o1', ?, '[\"a@example.com\"]', '旧主题', ?, '2026-09-01T00:00:00+00:00')",
                (version, body),
            )

    assert init_db() == SCHEMA_VERSION
    tasks, drafts = SessionStore(), ReplyDraftStore(valid)
    assert tasks.get_task("t1")["sdk_session_id"] == "sdk-old"
    assert drafts.get_reply_draft("o1", 1)["body"] == "第一版"
    assert drafts.get_reply_draft("o1", 2)["body"] == "第二版"

    sender = Sender()
    response = ConfirmationService(sender).confirm_reply("t1", "o1", 2)
    assert response["status"] == "sent"
    assert response["confirmation"]["task_id"] == "t1"

    assert init_db() == SCHEMA_VERSION
    assert drafts.get_reply_draft("o1", 1)["body"] == "第一版"
    assert ConfirmationService(sender).get_execution("o1") == response
    assert tasks.list_task_operations("t1") == [
        {"operation_id": "o1", "type": "mail_reply", "version": 2, "status": "sent"}
    ]


def test_execution_record_constraints(stores):
    task, operation = prepare(stores)
    with session() as conn, write(conn):
        conn.execute(
            "INSERT INTO approval_executions (operation_id, task_id, version, confirmed_at) "
            "VALUES (?, ?, 1, '2026-09-12T00:00:00+00:00')",
            (operation["operation_id"], task["task_id"]),
        )
    with session() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        statements = [
            (
                "INSERT INTO approval_executions (operation_id, task_id, version, confirmed_at) "
                "VALUES (?, ?, 1, 'now')",
                (operation["operation_id"], task["task_id"]),
            ),
            (
                "INSERT INTO approval_executions (operation_id, task_id, version, confirmed_at) "
                "VALUES ('missing', ?, 1, 'now')",
                (task["task_id"],),
            ),
            # 执行未结束时结果字段必须为空
            (
                "UPDATE approval_executions SET message_id = 'm' WHERE operation_id = ?",
                (operation["operation_id"],),
            ),
        ]
        for statement, params in statements:
            with pytest.raises(sqlite3.IntegrityError), write(conn):
                conn.execute(statement, params)
