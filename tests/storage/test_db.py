"""连接纪律：Approval 取得执行权依赖写事务互斥与失败回滚。"""

import json
import sqlite3

import pytest

from server import db
from server.config import Settings
from server.db import SCHEMA_VERSION, init_db, schema_version, session, write


def test_schema_version_survives_reconnect(settings: Settings) -> None:
    assert init_db() == SCHEMA_VERSION

    with session() as conn:
        assert schema_version(conn) == SCHEMA_VERSION

    assert settings.db_path.exists()


def test_write_holds_exclusive_lock(settings: Settings) -> None:
    init_db()

    # busy_timeout=0：竞争者必须立刻失败，而不是排队等写锁释放。
    with (
        session() as holder,
        write(holder),
        session(busy_timeout_ms=0) as contender,
        pytest.raises(sqlite3.OperationalError, match="locked"),
        write(contender),
    ):
        pass


def test_write_rolls_back_on_error(settings: Settings) -> None:
    init_db()

    with session() as conn, pytest.raises(RuntimeError), write(conn):
        conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION + 1,))
        raise RuntimeError("执行中断")

    with session() as conn:
        assert schema_version(conn) == SCHEMA_VERSION


def test_v7_migration_preserves_completed_mail_result(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn, write(conn):
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES (6)")
        for version in range(1, 7):
            for statement in db.SCHEMA_MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute("INSERT INTO tasks VALUES ('t1','旧任务',NULL,'2026-09-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO operations VALUES "
            "('o1','mail','t1',1,'sent','2026-09-01T00:00:00Z','2026-09-01T00:01:00Z')"
        )
        conn.execute("INSERT INTO task_operations VALUES ('t1','o1')")
        conn.execute(
            "INSERT INTO approval_executions "
            "(operation_id,task_id,version,confirmed_at,message_id,completed_at,started_at) "
            "VALUES ('o1','t1',1,'2026-09-01T00:00:00Z','message-1',"
            "'2026-09-01T00:01:00Z','2026-09-01T00:00:30Z')"
        )

    assert init_db() == SCHEMA_VERSION
    with session() as conn:
        row = conn.execute(
            "SELECT result_json FROM approval_executions WHERE operation_id='o1'"
        ).fetchone()
        assert json.loads(row["result_json"]) == {"status": "sent", "message_id": "message-1"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v9_migration_adds_memory_reviews_table(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn, write(conn):
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES (8)")
        for version in range(1, 9):
            for statement in db.SCHEMA_MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute("INSERT INTO tasks VALUES ('t1','任务',NULL,'2026-09-01T00:00:00Z')")

    assert init_db() == SCHEMA_VERSION
    with session() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            "INSERT INTO memory_reviews (review_id, task_id, status, origin, from_rowid, "
            "through_rowid, created_at) VALUES ('r1','t1','pending','manual',0,0,"
            "'2026-09-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_reviews (review_id, task_id, status, origin, from_rowid, "
                "through_rowid, created_at) VALUES ('r2','t1','pending','manual',0,0,"
                "'2026-09-01T00:00:00Z')"
            )
        # 已结束的回顾不阻止下一次登记。
        conn.execute("UPDATE memory_reviews SET status='done', finished_at='2026-09-01T00:01:00Z'")
        conn.execute(
            "INSERT INTO memory_reviews (review_id, task_id, status, origin, from_rowid, "
            "through_rowid, created_at) VALUES ('r3','t1','pending','interval',0,0,"
            "'2026-09-01T00:02:00Z')"
        )


def test_v11_migration_adds_run_sources_without_touching_existing_timeline(
    settings: Settings,
) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn, write(conn):
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES (10)")
        for version in range(1, 11):
            for statement in db.SCHEMA_MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute("INSERT INTO tasks VALUES ('t1','任务',NULL,'2026-09-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO agent_runs "
            "(run_id, task_id, kind, input, status, created_at, finished_at) "
            "VALUES ('run1','t1','message','{}','done','2026-09-01T00:00:00Z',"
            "'2026-09-01T00:00:01Z')"
        )
        conn.execute(
            "INSERT INTO task_timeline_items "
            "(item_id, task_id, run_id, kind, role, text, operation_id, created_at) "
            "VALUES ('i1','t1','run1','text','assistant','旧回答',NULL,'2026-09-01T00:00:00Z')"
        )

    assert init_db() == SCHEMA_VERSION
    with session() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT text FROM task_timeline_items WHERE item_id='i1'"
        ).fetchone()["text"] == "旧回答"
        source = (
            "INSERT INTO task_run_sources (source_id, task_id, run_id, sequence, doc_id, path, "
            "title, heading, start_line, end_line, commit_sha, excerpt, created_at) "
            "VALUES (?, 't1', 'run1', 1, 'kb_1', 'kb/inbox/a.md', '资料', '资料 / 分节', "
            "5, 7, 'abc', '原文片段', '2026-09-01T00:00:00Z')"
        )
        conn.execute(source, ("s1",))
        # 同一轮同一版本与行号只记一次
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(source, ("s2",))
        # 删除任务时来源一并清理
        conn.execute("DELETE FROM task_run_sources WHERE task_id='t1'")
        conn.execute("DELETE FROM task_timeline_items WHERE task_id='t1'")
        conn.execute("DELETE FROM agent_runs WHERE task_id='t1'")
        conn.execute("DELETE FROM tasks WHERE task_id='t1'")
        assert conn.execute("SELECT COUNT(*) AS n FROM task_run_sources").fetchone()["n"] == 0
