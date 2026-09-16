"""SQLite 连接、事务及 schema 初始化。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from server.config import get_settings

SCHEMA_VERSION = 13

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

# 确认执行记录：主键即操作标识，同一操作至多一份。结果字段只在执行结束时一起写入，
# 执行中 completed_at 为空；操作状态仍保存在 operations.status。
SCHEMA_V2 = (
    "CREATE TABLE approval_executions (operation_id TEXT PRIMARY KEY "
    "REFERENCES operations(operation_id), task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "version INTEGER NOT NULL CHECK(version >= 1), confirmed_at TEXT NOT NULL, "
    "message_id TEXT, reason TEXT, completed_at TEXT, "
    "CHECK ((completed_at IS NULL) = (message_id IS NULL AND reason IS NULL)))",
)

# 后台调用记录：每次 Agent 输入（新邮件、用户消息、执行结果回传）一条，保存类别、
# 必要输入与运行状态，供查询和重启识别。执行结果回传按操作标识唯一，重复确认不新增回传。
SCHEMA_V3 = (
    "CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "kind TEXT NOT NULL CHECK(kind IN ('new_mail','message','execution_result')), "
    "reference_id TEXT, input TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('pending','running','done','error','interrupted')), "
    "error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
    "CHECK ((finished_at IS NULL) = (status IN ('pending','running'))), "
    "CHECK (error IS NULL OR status IN ('error','interrupted')))",
    "CREATE UNIQUE INDEX agent_runs_delivery ON agent_runs(reference_id) "
    "WHERE kind = 'execution_result'",
    "CREATE TABLE mail_task_links (source_message_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), created_at TEXT NOT NULL)",
    # 已接受确认的发送由后台执行；started_at 标记执行已开始，重复调度不再发送。
    "ALTER TABLE approval_executions ADD COLUMN started_at TEXT",
)

# 新邮件与回复共用草稿表；回复的原邮件关联在迁移中原样保留。
SCHEMA_V4 = (
    "CREATE TABLE mail_drafts (operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id), "
    "kind TEXT NOT NULL CHECK(kind IN ('reply','new')), source_message_id TEXT UNIQUE, "
    "thread_id TEXT, CHECK ((kind = 'reply') = "
    "(source_message_id IS NOT NULL AND thread_id IS NOT NULL)))",
    "CREATE TABLE mail_draft_versions (operation_id TEXT NOT NULL "
    "REFERENCES mail_drafts(operation_id), version INTEGER NOT NULL CHECK(version >= 1), "
    "recipients TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, "
    "created_at TEXT NOT NULL, PRIMARY KEY(operation_id, version))",
    "INSERT INTO mail_drafts SELECT operation_id, 'reply', source_message_id, thread_id "
    "FROM mail_reply_drafts",
    "INSERT INTO mail_draft_versions SELECT * FROM mail_reply_versions",
    "DROP TABLE mail_reply_versions",
    "DROP TABLE mail_reply_drafts",
    "UPDATE operations SET type = 'mail' WHERE type = 'mail_reply'",
)

# 网页展示使用应用持久化的有序时间线；SDK 会话只负责模型上下文恢复。
# 上传文件使用不可变内部标识保存，草稿版本只绑定有序标识列表，不保存用户文件名路径。
SCHEMA_V5 = (
    "CREATE TABLE uploaded_files (file_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), filename TEXT NOT NULL, "
    "mime_type TEXT NOT NULL, size INTEGER NOT NULL CHECK(size >= 0), sha256 TEXT NOT NULL, "
    "storage_path TEXT NOT NULL, created_at TEXT NOT NULL)",
    "ALTER TABLE mail_draft_versions ADD COLUMN attachment_ids TEXT NOT NULL DEFAULT '[]'",
    "CREATE TABLE task_timeline_items (item_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "kind TEXT NOT NULL CHECK(kind IN ('text','mail_draft','error')), "
    "role TEXT CHECK(role IN ('user','assistant')), text TEXT, "
    "operation_id TEXT REFERENCES operations(operation_id), "
    "attachment_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, "
    "CHECK ((kind = 'text') = (role IS NOT NULL AND text IS NOT NULL)), "
    "CHECK ((kind = 'mail_draft') = (operation_id IS NOT NULL)), "
    "CHECK (kind != 'error' OR (role IS NULL AND text IS NOT NULL)))",
    "CREATE UNIQUE INDEX task_timeline_mail_draft "
    "ON task_timeline_items(task_id, operation_id) WHERE kind = 'mail_draft'",
)

# 邮件只支持纯文字：移除上传文件与草稿、时间线上的附件绑定。
SCHEMA_V6 = (
    "DROP TABLE uploaded_files",
    "ALTER TABLE mail_draft_versions DROP COLUMN attachment_ids",
    "ALTER TABLE task_timeline_items DROP COLUMN attachment_ids",
)

# 日历预览加入现有操作、确认和时间线模型；确认结果改为按域保存的统一 JSON。
SCHEMA_V7 = (
    SCHEMA_V1[1]
    .replace("CREATE TABLE operations", "CREATE TABLE operations_new")
    .replace("'sending','sent'", "'sending','sent','creating','created'"),
    "INSERT INTO operations_new SELECT * FROM operations",
    "DROP TABLE operations",
    "ALTER TABLE operations_new RENAME TO operations",
    "CREATE TABLE approval_executions_new (operation_id TEXT PRIMARY KEY "
    "REFERENCES operations(operation_id), task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "version INTEGER NOT NULL CHECK(version >= 1), confirmed_at TEXT NOT NULL, "
    "started_at TEXT, completed_at TEXT, result_json TEXT, "
    "CHECK ((completed_at IS NULL) = (result_json IS NULL)))",
    "INSERT INTO approval_executions_new "
    "SELECT e.operation_id,e.task_id,e.version,e.confirmed_at,e.started_at,e.completed_at,"
    "CASE WHEN e.completed_at IS NULL THEN NULL "
    "WHEN e.message_id IS NOT NULL THEN json_object('status','sent','message_id',e.message_id) "
    "ELSE json_object('status',o.status,'reason',e.reason) END "
    "FROM approval_executions e JOIN operations o ON o.operation_id=e.operation_id",
    "DROP TABLE approval_executions",
    "ALTER TABLE approval_executions_new RENAME TO approval_executions",
    "CREATE TABLE task_timeline_items_new (item_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "kind TEXT NOT NULL CHECK(kind IN ('text','mail_draft','calendar_preview','error')), "
    "role TEXT CHECK(role IN ('user','assistant')), text TEXT, "
    "operation_id TEXT REFERENCES operations(operation_id), created_at TEXT NOT NULL, "
    "CHECK ((kind = 'text') = (role IS NOT NULL AND text IS NOT NULL)), "
    "CHECK ((kind IN ('mail_draft','calendar_preview')) = (operation_id IS NOT NULL)), "
    "CHECK (kind != 'error' OR (role IS NULL AND text IS NOT NULL)))",
    "INSERT INTO task_timeline_items_new SELECT * FROM task_timeline_items",
    "DROP TABLE task_timeline_items",
    "ALTER TABLE task_timeline_items_new RENAME TO task_timeline_items",
    "CREATE UNIQUE INDEX task_timeline_mail_draft ON task_timeline_items(task_id,operation_id) "
    "WHERE kind='mail_draft'",
    "CREATE UNIQUE INDEX task_timeline_calendar_preview "
    "ON task_timeline_items(task_id,operation_id) "
    "WHERE kind='calendar_preview'",
    "CREATE TABLE calendar_previews (operation_id TEXT PRIMARY KEY "
    "REFERENCES operations(operation_id), "
    "calendar_id TEXT NOT NULL CHECK(calendar_id='primary'), account TEXT NOT NULL, "
    "calendar_url TEXT NOT NULL)",
    "CREATE TABLE calendar_preview_versions (operation_id TEXT NOT NULL "
    "REFERENCES calendar_previews(operation_id), version INTEGER NOT NULL CHECK(version >= 1), "
    "summary TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, "
    "all_day INTEGER NOT NULL CHECK(all_day IN (0,1)), location TEXT, description TEXT NOT NULL, "
    "created_at TEXT NOT NULL, PRIMARY KEY(operation_id,version))",
)

# 日程取消待确认预览与时间线卡片：创建只在用户对话轮直接发生，冲突经对话询问解决。
# 历史日程卡片从时间线移除（操作与执行记录保留），内容版本表改名跟随产品概念。
SCHEMA_V8 = (
    "DROP INDEX task_timeline_calendar_preview",
    "DELETE FROM task_timeline_items WHERE kind='calendar_preview'",
    "CREATE TABLE task_timeline_items_new (item_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "kind TEXT NOT NULL CHECK(kind IN ('text','mail_draft','error')), "
    "role TEXT CHECK(role IN ('user','assistant')), text TEXT, "
    "operation_id TEXT REFERENCES operations(operation_id), created_at TEXT NOT NULL, "
    "CHECK ((kind = 'text') = (role IS NOT NULL AND text IS NOT NULL)), "
    "CHECK ((kind = 'mail_draft') = (operation_id IS NOT NULL)), "
    "CHECK (kind != 'error' OR (role IS NULL AND text IS NOT NULL)))",
    "INSERT INTO task_timeline_items_new SELECT * FROM task_timeline_items",
    "DROP TABLE task_timeline_items",
    "ALTER TABLE task_timeline_items_new RENAME TO task_timeline_items",
    "CREATE UNIQUE INDEX task_timeline_mail_draft ON task_timeline_items(task_id,operation_id) "
    "WHERE kind='mail_draft'",
    "ALTER TABLE calendar_previews RENAME TO calendar_events",
    "ALTER TABLE calendar_preview_versions RENAME TO calendar_event_versions",
)

# 后台记忆回顾：按任务记录每次回顾的覆盖范围与状态，独立于 agent_runs，不进入任务时间线。
# through_rowid 是本次回顾覆盖到的 agent_runs 行号（软引用，任务删除时一并清理）。
# origin 区分周期触发与手动触发；每个任务至多一条待处理或运行中的回顾，由部分唯一索引强制。
SCHEMA_V9 = (
    "CREATE TABLE memory_reviews (review_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "status TEXT NOT NULL CHECK(status IN ('pending','running','done','error','interrupted')), "
    "origin TEXT NOT NULL CHECK(origin IN ('interval','manual')), "
    "from_rowid INTEGER NOT NULL CHECK(from_rowid >= 0), "
    "through_rowid INTEGER NOT NULL CHECK(through_rowid >= from_rowid), "
    "error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
    "CHECK ((finished_at IS NULL) = (status IN ('pending','running'))), "
    "CHECK (error IS NULL OR status IN ('error','interrupted')))",
    "CREATE UNIQUE INDEX memory_reviews_open ON memory_reviews(task_id) "
    "WHERE status IN ('pending','running')",
)

# 程序提示：记忆判断产生的“已记住/已修改/想确认”等提示是时间线上的独立一类，
# 不由模型输出，role 为空以区别于对话文本。重建表以放宽 kind 检查。
SCHEMA_V10 = (
    "CREATE TABLE task_timeline_items_new (item_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "kind TEXT NOT NULL CHECK(kind IN ('text','mail_draft','error','notice')), "
    "role TEXT CHECK(role IN ('user','assistant')), text TEXT, "
    "operation_id TEXT REFERENCES operations(operation_id), created_at TEXT NOT NULL, "
    "CHECK ((kind = 'text') = (role IS NOT NULL AND text IS NOT NULL)), "
    "CHECK ((kind = 'mail_draft') = (operation_id IS NOT NULL)), "
    "CHECK (kind NOT IN ('error','notice') OR (role IS NULL AND text IS NOT NULL)))",
    "INSERT INTO task_timeline_items_new SELECT * FROM task_timeline_items",
    "DROP TABLE task_timeline_items",
    "ALTER TABLE task_timeline_items_new RENAME TO task_timeline_items",
    "CREATE UNIQUE INDEX task_timeline_mail_draft ON task_timeline_items(task_id,operation_id) "
    "WHERE kind='mail_draft'",
)

# 回答的来源：模型成功读取资料原文时按轮次记录引用与本次实际读到的片段，供时间线在
# 该轮最后一段回答下展示。同一轮重复读取同一版本与行号只记一次；摘要不作为来源。
SCHEMA_V11 = (
    "CREATE TABLE task_run_sources (source_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "sequence INTEGER NOT NULL CHECK(sequence >= 1), "
    "doc_id TEXT NOT NULL, path TEXT NOT NULL, title TEXT, heading TEXT, "
    "start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, commit_sha TEXT NOT NULL, "
    "excerpt TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE UNIQUE INDEX task_run_sources_ref "
    "ON task_run_sources(run_id, commit_sha, path, start_line, end_line)",
)

# 撤销回答来源：产品不再向用户展示资料来源，来源记录表随之删除。
SCHEMA_V12 = ("DROP TABLE task_run_sources",)

# 历史对话检索：时间线条目的全文索引，是可从时间线重建的派生数据。检索前按需增量同步——
# 已结束轮次里还没进索引的条目补进来，内容或执行状态变了的邮件草稿重写——不挂写入钩子。
# 不设外键：删除任务时按任务清理，不受子表删除顺序约束。
SCHEMA_V13 = (
    "CREATE TABLE history_items (item_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
    "fts_rowid INTEGER NOT NULL UNIQUE, op_version INTEGER, op_status TEXT)",
    "CREATE INDEX history_items_task ON history_items(task_id)",
    "CREATE VIRTUAL TABLE history_fts USING fts5(body, tokenize='trigram')",
)

SCHEMA_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
    5: SCHEMA_V5,
    6: SCHEMA_V6,
    7: SCHEMA_V7,
    8: SCHEMA_V8,
    9: SCHEMA_V9,
    10: SCHEMA_V10,
    11: SCHEMA_V11,
    12: SCHEMA_V12,
    13: SCHEMA_V13,
}

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
    """建立实例持久目录、启用 WAL，按版本补建缺失的表并记录 schema 版本，返回当前版本。

    升级语句与版本号在同一写事务内提交：中途失败时现有数据与版本号都不变。
    已经是当前版本时不执行任何语句，业务记录不受重复初始化影响。
    """
    target = path or get_settings().db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with session(target) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        with write(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
            if conn.execute("SELECT COUNT(*) AS n FROM schema_meta").fetchone()["n"] == 0:
                conn.execute("INSERT INTO schema_meta (version) VALUES (0)")
            current = schema_version(conn)
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported schema version: {current}")
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in SCHEMA_MIGRATIONS[version]:
                    conn.execute(statement)
            if current != SCHEMA_VERSION:
                conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Migration foreign key check failed")
        conn.execute("PRAGMA foreign_keys=ON")
        return schema_version(conn)
