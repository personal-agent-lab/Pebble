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
