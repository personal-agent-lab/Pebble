"""SQLite 连接、事务及 schema 初始化。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from server.config import default_model, get_settings

SCHEMA_VERSION = 23

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

# 程序提示（如后台记忆回顾结果）是时间线上的独立一类，不由模型输出，
# role 为空以区别于对话文本。重建表以放宽 kind 检查。
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

# 任务固定模型；附件使用任务内不可变标识，时间线只保存有序关联。
SCHEMA_V14 = (
    "ALTER TABLE tasks ADD COLUMN model TEXT NOT NULL DEFAULT 'auto'",
    "CREATE TABLE uploaded_files (file_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), filename TEXT NOT NULL, "
    "mime_type TEXT NOT NULL, size INTEGER NOT NULL CHECK(size >= 0), sha256 TEXT NOT NULL, "
    "storage_path TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX uploaded_files_task ON uploaded_files(task_id)",
    "CREATE TABLE timeline_item_attachments ("
    "item_id TEXT NOT NULL REFERENCES task_timeline_items(item_id), "
    "file_id TEXT NOT NULL REFERENCES uploaded_files(file_id), position INTEGER NOT NULL, "
    "PRIMARY KEY(item_id,file_id), UNIQUE(item_id,position))",
)

# 用户可以取消待确认的邮件草稿：cancelled 是不产生外部写入的终止状态，没有执行记录。
SCHEMA_V15 = (
    SCHEMA_V7[0].replace("'created'", "'created','cancelled'"),
    "INSERT INTO operations_new SELECT * FROM operations",
    "DROP TABLE operations",
    "ALTER TABLE operations_new RENAME TO operations",
)

# 同一原邮件的回复被用户取消后，再起草时新建操作与卡片，不复用已取消的那份：
# 原邮件标识不再唯一，“每封原邮件至多一份未取消的回复”由保存草稿的写事务保证。
SCHEMA_V16 = (
    SCHEMA_V4[0]
    .replace("CREATE TABLE mail_drafts", "CREATE TABLE mail_drafts_new")
    .replace("source_message_id TEXT UNIQUE", "source_message_id TEXT"),
    "INSERT INTO mail_drafts_new SELECT * FROM mail_drafts",
    "DROP TABLE mail_drafts",
    "ALTER TABLE mail_drafts_new RENAME TO mail_drafts",
    "CREATE INDEX mail_drafts_source ON mail_drafts(source_message_id)",
)

# Skills migrations follow main's existing versions; never reuse 9/10.
# Some development databases already contain these tables under main's version 16.
SCHEMA_V17 = (
    "CREATE TABLE IF NOT EXISTS skill_run_links ("
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "skill_id TEXT NOT NULL, revision TEXT NOT NULL, "
    "source TEXT NOT NULL CHECK(source IN ('manual', 'auto')), "
    "loaded_at TEXT NOT NULL, PRIMARY KEY(run_id, skill_id))",
    "CREATE TABLE IF NOT EXISTS skill_draft_evidence (draft_id TEXT NOT NULL, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), created_at TEXT NOT NULL, "
    "PRIMARY KEY(draft_id, task_id))",
)

SCHEMA_V18 = (
    "CREATE TABLE IF NOT EXISTS skill_tool_evidence (id TEXT PRIMARY KEY, "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE, "
    "tool_name TEXT NOT NULL, argument_keys TEXT NOT NULL, succeeded INTEGER NOT NULL, "
    "created_at TEXT NOT NULL)",
)

# Skills 重写（docs/skills.md，2026-09）：旧设计的运行表全部退役，正文不在 SQLite。
# skill_changes 记录“提出与应用分离”的每次变更；skill_loads 是进入材料与 skill_view 的加载记录。
SCHEMA_V19 = (
    "DROP TABLE IF EXISTS skill_tool_evidence",
    "DROP TABLE IF EXISTS skill_draft_evidence",
    "DROP TABLE IF EXISTS skill_run_links",
    "CREATE TABLE skill_changes (id TEXT PRIMARY KEY, "
    "review_job_id TEXT, skill_id TEXT, "
    "action TEXT NOT NULL CHECK(action IN ('create','patch','write_file','remove_file')), "
    "payload TEXT NOT NULL, base_revision TEXT, reason TEXT NOT NULL, "
    "evidence_item_ids TEXT NOT NULL, "
    "actor TEXT NOT NULL CHECK(actor IN ('user','foreground','review')), "
    "status TEXT NOT NULL CHECK(status IN ('proposed','applied','rejected','conflict')), "
    "created_at TEXT NOT NULL, applied_at TEXT)",
    "CREATE INDEX skill_changes_skill ON skill_changes(skill_id, created_at)",
    "CREATE TABLE skill_loads (run_id TEXT NOT NULL, task_id TEXT NOT NULL, "
    "skill_id TEXT NOT NULL, revision TEXT NOT NULL, "
    "source TEXT NOT NULL CHECK(source IN ('manual', 'auto')), "
    "loaded_at TEXT NOT NULL, PRIMARY KEY(run_id, skill_id))",
    "CREATE INDEX skill_loads_skill ON skill_loads(skill_id, loaded_at)",
)

# 执行轨迹（docs/observability.md §2）：时间线补任务内单调 sequence，并新增 tool 条目
# 承载每次工具调用——工具名、参数（含值）、成败与返回内容。轨迹条目留在时间线里，
# evidence_item_ids 才能指向真实条目；参数与返回随轨迹只存本地 SQLite。
SCHEMA_V20 = (
    "CREATE TABLE task_timeline_items_new (item_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "sequence INTEGER NOT NULL CHECK(sequence >= 0), "
    "kind TEXT NOT NULL CHECK(kind IN ('text','mail_draft','error','notice','tool')), "
    "role TEXT CHECK(role IN ('user','assistant')), text TEXT, "
    "operation_id TEXT REFERENCES operations(operation_id), "
    "tool_call_id TEXT, tool_name TEXT, tool_arguments TEXT, "
    "tool_status TEXT CHECK(tool_status IN ('ok','error')), tool_result TEXT, "
    "created_at TEXT NOT NULL, "
    "CHECK ((kind = 'text') = (role IS NOT NULL AND text IS NOT NULL)), "
    "CHECK ((kind = 'mail_draft') = (operation_id IS NOT NULL)), "
    "CHECK (kind NOT IN ('error','notice') OR (role IS NULL AND text IS NOT NULL)), "
    "CHECK ((kind = 'tool') = (tool_call_id IS NOT NULL AND tool_name IS NOT NULL "
    "AND tool_arguments IS NOT NULL AND tool_status IS NOT NULL)), "
    "CHECK (kind != 'tool' OR (role IS NULL AND text IS NULL AND operation_id IS NULL "
    "AND tool_result IS NOT NULL)))",
    "INSERT INTO task_timeline_items_new (item_id, task_id, run_id, sequence, kind, role, "
    "text, operation_id, created_at) "
    "SELECT item_id, task_id, run_id, rowid, kind, role, text, operation_id, created_at "
    "FROM task_timeline_items",
    "DROP TABLE task_timeline_items",
    "ALTER TABLE task_timeline_items_new RENAME TO task_timeline_items",
    "CREATE UNIQUE INDEX task_timeline_mail_draft ON task_timeline_items(task_id,operation_id) "
    "WHERE kind='mail_draft'",
    "CREATE UNIQUE INDEX task_timeline_sequence ON task_timeline_items(task_id, sequence)",
)

# 工具开始时即占据轨迹位置；结果到达后更新同一行。旧记录原样迁移。
SCHEMA_V21 = (
    "CREATE TABLE task_timeline_items_new (item_id TEXT PRIMARY KEY, "
    "task_id TEXT NOT NULL REFERENCES tasks(task_id), "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id), "
    "sequence INTEGER NOT NULL CHECK(sequence >= 0), "
    "kind TEXT NOT NULL CHECK(kind IN ('text','mail_draft','error','notice','tool')), "
    "role TEXT CHECK(role IN ('user','assistant')), text TEXT, "
    "operation_id TEXT REFERENCES operations(operation_id), "
    "tool_call_id TEXT, tool_name TEXT, tool_arguments TEXT, "
    "tool_status TEXT CHECK(tool_status IN ('running','ok','error')), tool_result TEXT, "
    "created_at TEXT NOT NULL, "
    "CHECK ((kind = 'text') = (role IS NOT NULL AND text IS NOT NULL)), "
    "CHECK ((kind = 'mail_draft') = (operation_id IS NOT NULL)), "
    "CHECK (kind NOT IN ('error','notice') OR (role IS NULL AND text IS NOT NULL)), "
    "CHECK ((kind = 'tool') = (tool_call_id IS NOT NULL AND tool_name IS NOT NULL "
    "AND tool_arguments IS NOT NULL AND tool_status IS NOT NULL)), "
    "CHECK (kind != 'tool' OR (role IS NULL AND text IS NULL AND operation_id IS NULL "
    "AND ((tool_status = 'running' AND tool_result IS NULL) "
    "OR (tool_status IN ('ok','error') AND tool_result IS NOT NULL)))))",
    "INSERT INTO task_timeline_items_new SELECT * FROM task_timeline_items",
    "DROP TABLE task_timeline_items",
    "ALTER TABLE task_timeline_items_new RENAME TO task_timeline_items",
    "CREATE UNIQUE INDEX task_timeline_mail_draft ON task_timeline_items(task_id,operation_id) "
    "WHERE kind='mail_draft'",
    "CREATE UNIQUE INDEX task_timeline_sequence ON task_timeline_items(task_id, sequence)",
    "CREATE UNIQUE INDEX task_timeline_tool_call ON task_timeline_items(run_id, tool_call_id) "
    "WHERE kind='tool'",
)

# 运行观测（docs/observability.md）：run_observations 是一轮的运行摘要，
# observation_steps 记本轮的工具尝试、压缩与静默降级；都从属 agent_runs，删轮次时级联删除。
# 工具参数与返回正文只在时间线存一份，这里只存引用与规模。
SCHEMA_V22 = (
    "CREATE TABLE run_observations (run_id TEXT PRIMARY KEY "
    "REFERENCES agent_runs(run_id) ON DELETE CASCADE, "
    "materials TEXT, sdk_result TEXT, context_before TEXT, context_after TEXT, "
    "updated_at TEXT NOT NULL)",
    "CREATE TABLE observation_steps (step_id TEXT PRIMARY KEY, "
    "run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE, "
    "kind TEXT NOT NULL CHECK(kind IN ('tool','compact','degraded')), "
    "code TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('running','ok','error','denied')), "
    "started_at TEXT, ended_at TEXT, item_id TEXT, tool_call_id TEXT, detail TEXT)",
    "CREATE INDEX observation_steps_run ON observation_steps(run_id)",
    "CREATE UNIQUE INDEX observation_steps_tool ON observation_steps(run_id, tool_call_id) "
    "WHERE kind='tool'",
)

# Skill 后台复盘：完成事件按提交顺序编号，避免并发任务的创建顺序冒充完成顺序。
# 复盘候选先落库，再按序应用；中断后可从未处理的候选继续。
SCHEMA_V23 = (
    "CREATE TABLE skill_review_turns (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "run_id TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE skill_review_state (id INTEGER PRIMARY KEY CHECK(id=1), "
    "cursor_seq INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0)",
    "INSERT INTO skill_review_state (id) VALUES (1)",
    "CREATE TABLE skill_reviews (id TEXT PRIMARY KEY, from_seq INTEGER NOT NULL, "
    "through_seq INTEGER NOT NULL, target_seq INTEGER NOT NULL, generation INTEGER NOT NULL, "
    "anchor_task_id TEXT NOT NULL, status TEXT NOT NULL "
    "CHECK(status IN ('pending','running','applying','completed','failed')), "
    "result_summary TEXT, error TEXT, retry_after_seq INTEGER, "
    "created_at TEXT NOT NULL, finished_at TEXT)",
    "CREATE UNIQUE INDEX skill_reviews_open ON skill_reviews((1)) "
    "WHERE status IN ('pending','running','applying')",
    "CREATE TABLE skill_review_candidates (review_id TEXT NOT NULL REFERENCES skill_reviews(id), "
    "ordinal INTEGER NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL, "
    "skill_id TEXT, base_revision TEXT, reason TEXT NOT NULL, evidence_item_ids TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('pending','applied','proposed','conflict','failed')), "
    "change_id TEXT, error TEXT, PRIMARY KEY(review_id, ordinal))",
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
    14: SCHEMA_V14,
    15: SCHEMA_V15,
    16: SCHEMA_V16,
    17: SCHEMA_V17,
    18: SCHEMA_V18,
    19: SCHEMA_V19,
    20: SCHEMA_V20,
    21: SCHEMA_V21,
    22: SCHEMA_V22,
    23: SCHEMA_V23,
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
                if version == 14:
                    # 旧任务此前逐轮使用全局配置；迁移时把当下有效型号固化到任务。
                    conn.execute(
                        "UPDATE tasks SET model = ?",
                        (default_model(),),
                    )
            if current != SCHEMA_VERSION:
                conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Migration foreign key check failed")
        conn.execute("PRAGMA foreign_keys=ON")
        return schema_version(conn)
