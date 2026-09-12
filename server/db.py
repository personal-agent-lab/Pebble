"""SQLite 连接、事务及 schema 初始化。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from server.config import get_settings

SCHEMA_VERSION = 1

SCHEMA_V1 = (
    "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, goal TEXT NOT NULL, "
    "sdk_session_id TEXT, created_at TEXT NOT NULL)",
    "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, type TEXT NOT NULL, "
    "created_task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "version INTEGER NOT NULL CHECK(version >= 1), "
    "status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','unknown')), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE task_operations (task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "operation_id TEXT NOT NULL REFERENCES operations(operation_id), "
    "PRIMARY KEY(task_id, operation_id))",
    "CREATE TABLE mail_reply_drafts (operation_id TEXT PRIMARY KEY "
    "REFERENCES operations(operation_id), "
    "source_message_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL)",
    "CREATE TABLE mail_reply_versions (operation_id TEXT NOT NULL "
    "REFERENCES mail_reply_drafts(operation_id), version INTEGER NOT NULL CHECK(version >= 1), "
    "recipients TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, "
    "created_at TEXT NOT NULL, "
    "PRIMARY KEY(operation_id, version))",
)

DEFAULT_BUSY_TIMEOUT_MS = 5000


def connect(path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    # isolation_level=None 关闭 sqlite3 的隐式事务：写锁必须由 write() 显式用
    # BEGIN IMMEDIATE 取得，否则默认的 DEFERRED 事务会在升级写锁时抛 SQLITE_BUSY。
    conn = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return conn


@contextmanager
def session(
    path: Path | None = None, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection]:
    conn = connect(path or get_settings().db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """独占写事务，进入即持有写锁。

    Confirmation 取得执行权依赖这里的原子性：状态检查与更新必须在同一个
    IMMEDIATE 事务内完成，两个并发确认请求只能有一个拿到写锁。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT version FROM schema_meta").fetchone()["version"])


def init_db(path: Path | None = None) -> int:
    """建立实例持久目录、启用 WAL 并记录 schema 版本，返回当前版本。"""
    target = path or get_settings().db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with session(target) as conn:
        with write(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
            if conn.execute("SELECT COUNT(*) AS n FROM schema_meta").fetchone()["n"] == 0:
                conn.execute("INSERT INTO schema_meta (version) VALUES (0)")
            current = schema_version(conn)
            if current == 0:
                for statement in SCHEMA_V1:
                    conn.execute(statement)
                conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            elif current != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported schema version: {current}")
        return schema_version(conn)
