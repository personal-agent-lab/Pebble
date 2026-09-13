"""连接纪律：Approval 取得执行权依赖写事务互斥与失败回滚。"""

import sqlite3

import pytest

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
