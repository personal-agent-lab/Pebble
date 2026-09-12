"""真实落盘 SQLite；邮件业务校验仅使用显式测试替身。"""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from server.db import init_db, session, write
from server.sessions.errors import (
    NotEditableError,
    NotFoundError,
    SessionConflictError,
    VersionConflictError,
)
from server.sessions.service import SessionStore
from server.tools.gmail import repository as mail_repo
from server.tools.gmail.service import DraftValidationError, ReplyDraftStore

CONTENT = {
    "to": ["甲@example.com", "b@example.com"],
    "subject": " 回复：活动 ",
    "body": "你好\n\n谢谢！\n",
}


def valid(**kwargs):
    return {"valid": True, "errors": []}


@pytest.fixture
def stores(settings):
    init_db()
    return SessionStore(), ReplyDraftStore(valid)


def prepare(stores, source="m1"):
    tasks, drafts = stores
    task = tasks.create_task("处理我的请求")
    op = drafts.save_reply_draft(task["task_id"], source, "thread1", **CONTENT)
    return task, op


def test_tasks_and_binding(stores):
    tasks, _ = stores
    first = tasks.create_task("整理资料")
    second = tasks.create_task("安排时间")
    assert tasks.list_tasks() == [second, first]
    assert tasks.list_task_operations(first["task_id"]) == []
    assert set(first) == {"task_id", "goal", "sdk_session_id", "created_at"}
    for _ in range(2):
        assert tasks.bind_sdk_session(first["task_id"], "sdk1")["sdk_session_id"] == "sdk1"
    with pytest.raises(SessionConflictError):
        tasks.bind_sdk_session(first["task_id"], "sdk2")
    for call in (tasks.get_task, tasks.list_task_operations):
        with pytest.raises(NotFoundError):
            call("missing")


def test_versions_reuse_and_sessions(stores):
    tasks, drafts = stores
    task, op = prepare(stores)
    other = tasks.create_task("再次查看")
    tasks.bind_sdk_session(task["task_id"], "sdk1")
    tasks.bind_sdk_session(other["task_id"], "sdk2")
    reused = drafts.save_reply_draft(other["task_id"], "m1", "ignored", **CONTENT)
    assert reused == op
    edited = {**CONTENT, "body": "修改后\n"}
    assert drafts.update_reply_draft(op["operation_id"], 1, **edited)["version"] == 2
    for version, content in ((1, CONTENT), (2, edited)):
        result = drafts.get_reply_draft(op["operation_id"], version)
        assert {key: result[key] for key in content} == content
    for current_task in (task, other):
        assert tasks.list_task_operations(current_task["task_id"])[0]["version"] == 2
    assert tasks.get_task(task["task_id"])["sdk_session_id"] == "sdk1"
    assert tasks.get_task(other["task_id"])["sdk_session_id"] == "sdk2"
    with session() as conn:
        assert (
            conn.execute("SELECT created_task_id FROM operations").fetchone()[0] == task["task_id"]
        )
    with pytest.raises(VersionConflictError) as error:
        drafts.update_reply_draft(op["operation_id"], 1, **CONTENT)
    assert error.value.current_version == 2
    with pytest.raises(NotFoundError):
        drafts.get_reply_draft(op["operation_id"], 99)
    new = drafts.save_reply_draft(task["task_id"], "m2", "thread1", **CONTENT)
    assert new["operation_id"] != op["operation_id"]
    assert len(tasks.list_task_operations(task["task_id"])) == 2


def test_validation_failure_and_reuse(stores):
    task, op = prepare(stores)
    errors = [{"field": "to", "message": "不合法"}]
    calls = []

    def invalid(**kwargs):
        calls.append(kwargs)
        return {"valid": False, "errors": errors}

    drafts = ReplyDraftStore(invalid)
    assert drafts.save_reply_draft(task["task_id"], "m1", "ignored", **CONTENT) == op
    assert calls == []
    with pytest.raises(DraftValidationError) as error:
        drafts.update_reply_draft(op["operation_id"], 1, **CONTENT)
    assert error.value.errors == errors
    with pytest.raises(DraftValidationError):
        drafts.save_reply_draft(task["task_id"], "m2", "thread1", **CONTENT)
    assert drafts.get_reply_draft(op["operation_id"])["version"] == 1
    assert len(stores[0].list_task_operations(task["task_id"])) == 1


@pytest.mark.parametrize("status", ["sending", "sent", "failed", "unknown"])
def test_noneditable(stores, status):
    task, op = prepare(stores)
    with session() as conn, write(conn):
        conn.execute("UPDATE operations SET status = ?", (status,))
    drafts = stores[1]
    with pytest.raises(NotEditableError):
        drafts.update_reply_draft(op["operation_id"], 1, **CONTENT)
    assert drafts.get_reply_draft(op["operation_id"])["status"] == status
    assert drafts.save_reply_draft(task["task_id"], "m1", "thread1", **CONTENT)["status"] == status


def test_concurrent_edits(stores):
    _, op = prepare(stores)
    barrier = Barrier(2)

    def validate(**kwargs):
        barrier.wait(timeout=5)
        return valid()

    drafts = ReplyDraftStore(validate)

    def edit(body):
        try:
            drafts.update_reply_draft(op["operation_id"], 1, **{**CONTENT, "body": body})
            return body
        except VersionConflictError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(edit, ["first", "second"]))
    winners = [value for value in results if value is not None]
    assert len(winners) == 1
    assert drafts.get_reply_draft(op["operation_id"])["body"] == winners[0]
    assert drafts.get_reply_draft(op["operation_id"])["version"] == 2


def test_concurrent_creation(stores):
    tasks, _ = stores
    ids = [tasks.create_task(goal)["task_id"] for goal in ("first", "second")]
    barrier = Barrier(2)

    def validate(**kwargs):
        barrier.wait(timeout=5)
        return valid()

    drafts = ReplyDraftStore(validate)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda task_id: drafts.save_reply_draft(task_id, "m1", "t1", **CONTENT), ids)
        )
    assert results[0] == results[1]
    for task_id in ids:
        assert len(tasks.list_task_operations(task_id)) == 1
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1


@pytest.mark.parametrize("editing", [False, True])
def test_rollback(stores, monkeypatch, editing):
    tasks, drafts = stores
    task, op = prepare(stores)
    original = mail_repo.insert_version

    def interrupted(*args):
        original(*args)
        raise RuntimeError("interrupt before commit")

    monkeypatch.setattr(mail_repo, "insert_version", interrupted)
    with pytest.raises(RuntimeError):
        if editing:
            drafts.update_reply_draft(op["operation_id"], 1, **CONTENT)
        else:
            drafts.save_reply_draft(task["task_id"], "m2", "thread1", **CONTENT)
    assert drafts.get_reply_draft(op["operation_id"])["version"] == 1
    assert len(tasks.list_task_operations(task["task_id"])) == 1
    with session() as conn:
        for table in ("operations", "mail_reply_drafts", "mail_reply_versions", "task_operations"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_recheck_status_after_validation(stores):
    _, op = prepare(stores)

    def validate(**kwargs):
        with session() as conn, write(conn):
            conn.execute("UPDATE operations SET status = 'sending'")
        return valid()

    with pytest.raises(NotEditableError):
        ReplyDraftStore(validate).update_reply_draft(op["operation_id"], 1, **CONTENT)
    assert stores[1].get_reply_draft(op["operation_id"])["version"] == 1


def test_initialization_and_constraints(stores):
    task, op = prepare(stores)
    for _ in range(2):
        assert init_db() == 1
    assert stores[1].get_reply_draft(op["operation_id"])["body"] == CONTENT["body"]
    with session() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError), write(conn):
            conn.execute("INSERT INTO task_operations VALUES ('missing', ?)", (op["operation_id"],))
        with pytest.raises(sqlite3.IntegrityError), write(conn):
            conn.execute(
                "INSERT INTO task_operations VALUES (?, ?)", (task["task_id"], op["operation_id"])
            )


def test_cross_process(settings):
    common = """
import json
from server.db import init_db
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore
init_db()
tasks = SessionStore()
drafts = ReplyDraftStore(lambda **kw: {"valid": True, "errors": []})
"""
    writer = (
        common
        + """
t = tasks.create_task("跨进程目标")
tasks.bind_sdk_session(t["task_id"], "sdk-persisted")
o = drafts.save_reply_draft(t["task_id"], "m1", "thread1", ["a@x", "b@x"], "主题", "第一版")
drafts.update_reply_draft(o["operation_id"], 1, ["a@x", "b@x"], "主题", "第二版\\n正文")
result = {"task": tasks.get_task(t["task_id"]),
          "draft": drafts.get_reply_draft(o["operation_id"]),
          "ops": tasks.list_task_operations(t["task_id"])}
print(json.dumps(result))
"""
    )
    reader = (
        common
        + """
t = tasks.list_tasks()[0]
ops = tasks.list_task_operations(t["task_id"])
print(json.dumps({"task": t, "draft": drafts.get_reply_draft(ops[0]["operation_id"]), "ops": ops}))
"""
    )

    def run(code):
        return json.loads(
            subprocess.run(
                [sys.executable, "-c", code], check=True, capture_output=True, text=True
            ).stdout
        )

    expected = run(writer)
    assert run(reader) == expected
    assert settings.db_path.is_file()


def test_upgrade_zero_atomic(settings, monkeypatch):
    from server import db

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES (0)")
    statements = db.SCHEMA_V1
    monkeypatch.setattr(db, "SCHEMA_V1", (*statements, "INVALID SQL"))
    with pytest.raises(sqlite3.OperationalError):
        init_db()
    with session() as conn:
        assert db.schema_version(conn) == 0
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'tasks'").fetchone() is None
        )
    monkeypatch.setattr(db, "SCHEMA_V1", statements)
    assert init_db() == 1


def test_validator_cannot_rewrite_recipients(stores):
    tasks, _ = stores

    def validate(**kwargs):
        kwargs["to"].clear()
        return valid()

    drafts = ReplyDraftStore(validate)
    task = tasks.create_task("目标")
    op = drafts.save_reply_draft(task["task_id"], "m1", "t1", **CONTENT)
    assert drafts.get_reply_draft(op["operation_id"])["to"] == CONTENT["to"]
