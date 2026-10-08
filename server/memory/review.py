"""全局累计用户消息轮的后台记忆回顾；改删由工具转为独立询问对话。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from server.agent.context import Material, render_materials
from server.config import get_settings
from server.db import session, write
from server.errors import MemoryValidationError, NotFoundError
from server.memory.notices import MEMORY_RULES, memory_materials, review_notice_texts
from server.memory.proposals import MemoryProposals
from server.memory.service import MemoryStore
from server.sessions import repository, timeline
from server.sessions.service import timestamp

REVIEW_INSTRUCTIONS = (
    "你是 Pebble 的后台记忆整理程序，独立于用户对话运行，不面向用户回复。"
    "用户单轮明确表达的信息由前台 Agent 按需保存。"
    "你会收到一段对话记录和当前长期记忆，做两件事。\n"
    "\n"
    "补充跨多轮才看得出的信息，拿不准的不保存：\n"
    "- 重复出现的表达方式、语言、格式或工作节奏偏好，只出现一次的不算；\n"
    "- 被用户认可或纠正过、下次仍应沿用的做法；\n"
    "- 对话中逐渐明确的身份、长期目标、学习或研究方向。\n"
    "\n"
    "整理长期记忆：\n"
    "- 合并重复或相近的内容，精简冗长措辞，把放错分区的内容移回去；容量接近上限时优先整理。\n"
    "- 对话中有明确依据表明某项已过时或失效时更新或删除；没有明确依据不改动。\n"
    "\n"
    "纯新增直接保存；涉及替换、删除或移动时，将所有相关操作放在同一批次，提供 reason。"
    "工具会整批暂存并创建新的用户对话询问意见；staged 不代表记忆已经修改。"
    "不得拆开合并操作先添加再删除，也不得在后台应用或审批候选。\n"
    "没有要做的事时不调用工具，只回复“无”；完成后用一句话概括做了什么。\n"
    "\n"
    f"{MEMORY_RULES}"
)

REVIEW_MESSAGE_HEADER = "请按系统提示的规则审阅下面的材料，判断是否需要补充或整理长期记忆。"
REVIEW_INTERRUPTED_REASON = "上次进程退出时记忆回顾尚未结束，已记录中断"
REVIEW_EMPTY_TRANSCRIPT = "（无新增对话）"

REVIEW_FIELDS = (
    "review_id",
    "task_id",
    "status",
    "origin",
    "scope",
    "from_rowid",
    "through_rowid",
    "error",
    "created_at",
    "started_at",
    "finished_at",
)

INSERT_SQL = (
    "INSERT INTO memory_reviews "
    "(review_id, task_id, status, origin, from_rowid, through_rowid, created_at, scope) "
    "VALUES (?, ?, 'pending', ?, ?, ?, ?, 'global')"
)


class ReviewGateway(Protocol):
    """一次性记忆回顾调用：返回按顺序记录的工具调用与结果。"""

    async def review_memory(
        self, task_id: str, instructions: str, transcript: str
    ) -> list[dict]: ...


def review_response(row: dict) -> dict:
    result = {field: row[field] for field in REVIEW_FIELDS}
    if row.get("failure"):
        result["failure"] = json.loads(row["failure"])
    return result


def review(conn: sqlite3.Connection, review_id: str) -> dict:
    row = conn.execute("SELECT * FROM memory_reviews WHERE review_id = ?", (review_id,)).fetchone()
    if row is None:
        raise NotFoundError(review_id)
    return dict(row)


def insert_review(
    conn: sqlite3.Connection,
    review_id: str,
    task_id: str,
    origin: str,
    from_rowid: int,
    through_rowid: int,
    now: str,
) -> None:
    conn.execute(INSERT_SQL, (review_id, task_id, origin, from_rowid, through_rowid, now))


def pending_reviews(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM memory_reviews WHERE status = 'pending' "
            "AND scope = 'global' ORDER BY rowid"
        )
    ]


def claim_review(conn: sqlite3.Connection, review_id: str, now: str) -> dict | None:
    """标记回顾开始；只有待处理记录能取得运行权。"""
    cursor = conn.execute(
        "UPDATE memory_reviews SET status = 'running', started_at = ? "
        "WHERE review_id = ? AND status = 'pending'",
        (now, review_id),
    )
    if cursor.rowcount != 1:
        return None
    return review(conn, review_id)


def finish_review(
    conn: sqlite3.Connection, review_id: str, status: str, error: str | None, now: str
) -> None:
    # 锚点任务删除或重复收尾不推进全局检查点。
    record = conn.execute(
        "SELECT * FROM memory_reviews WHERE review_id = ?", (review_id,)
    ).fetchone()
    if record is None or record["status"] != "running":
        return
    if status == "done":
        row = dict(record)
        if row["scope"] == "global":
            conn.execute(
                "UPDATE memory_review_checkpoint SET through_rowid = MAX(through_rowid, ?) "
                "WHERE id = 1",
                (row["through_rowid"],),
            )
    conn.execute(
        "UPDATE memory_reviews SET status = ?, error = ?, finished_at = ? "
        "WHERE review_id = ? AND status = 'running'",
        (status, error, now, review_id),
    )


def interrupt_running_reviews(conn: sqlite3.Connection, now: str, reason: str) -> list[str]:
    review_ids = [
        row["review_id"]
        for row in conn.execute(
            "SELECT review_id FROM memory_reviews WHERE status = 'running' ORDER BY rowid"
        )
    ]
    for review_id in review_ids:
        conn.execute(
            "UPDATE memory_reviews SET status = 'interrupted', error = ?, finished_at = ? "
            "WHERE review_id = ?",
            (reason, now, review_id),
        )
    return review_ids


def open_review(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM memory_reviews WHERE scope = 'global' AND status IN ('pending','running') "
        "ORDER BY rowid LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


def sync_completed_turns(conn: sqlite3.Connection) -> None:
    # 自增序列与任务生命周期分离，不依赖删除后可能复用的 agent_runs.rowid。
    conn.execute(
        "INSERT OR IGNORE INTO memory_review_turns(run_id,kind) "
        "SELECT r.run_id,r.kind FROM agent_runs r WHERE r.status = 'done' "
        "AND json_extract(r.input, '$.memory_proposal_id') IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM memory_review_turns w WHERE w.run_id = r.run_id) "
        "ORDER BY r.rowid"
    )


def last_through_sequence(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT through_rowid FROM memory_review_checkpoint WHERE id = 1"
    ).fetchone()["through_rowid"]


def done_message_count_since(conn: sqlite3.Connection, through_rowid: int) -> int:
    sync_completed_turns(conn)
    return conn.execute(
        "SELECT COUNT(*) AS n FROM memory_review_turns WHERE kind = 'message' AND sequence > ?",
        (through_rowid,),
    ).fetchone()["n"]


def max_completed_sequence(conn: sqlite3.Connection) -> int:
    sync_completed_turns(conn)
    return conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS m FROM memory_review_turns"
    ).fetchone()["m"]


def last_run_id(conn: sqlite3.Connection, task_id: str, through_rowid: int) -> str | None:
    """窗口内最后一个调用；任务在回顾期间被删除时没有结果，提示随之不写。"""
    row = conn.execute(
        "SELECT r.run_id FROM agent_runs r JOIN memory_review_turns w ON w.run_id = r.run_id "
        "WHERE r.task_id = ? AND w.sequence <= ? ORDER BY w.sequence DESC LIMIT 1",
        (task_id, through_rowid),
    ).fetchone()
    return row["run_id"] if row is not None else None


def window_text_items(conn: sqlite3.Connection, from_rowid: int, through_rowid: int) -> list[dict]:
    """回顾窗口内的对话文本：只取窗口内已结束调用的 text 与 notice 项。

    notice 是程序生成的提示，作为对话上下文一起提供。
    草稿卡片与错误项不算对话。
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT i.kind, i.role, i.text, i.task_id FROM task_timeline_items i "
            "JOIN agent_runs r ON r.run_id = i.run_id "
            "JOIN memory_review_turns w ON w.run_id = r.run_id "
            "WHERE w.sequence > ? AND w.sequence <= ? AND r.status = 'done' "
            "AND i.kind IN ('text','notice') ORDER BY w.sequence, i.sequence",
            (from_rowid, through_rowid),
        )
    ]


def render_transcript(items: list[dict]) -> str:
    labels = {"user": "用户", "assistant": "助手"}
    return "\n".join(
        f"{'[任务 ' + item['task_id'] + '] ' if item.get('task_id') else ''}"
        f"{'系统' if item.get('kind') == 'notice' else labels.get(item['role'], item['role'])}"
        f"：{item['text']}"
        for item in items
    )


def build_review_message(transcript: str, snapshot: dict) -> str:
    materials = (
        Material("自上次回顾以来各任务的对话", transcript or REVIEW_EMPTY_TRANSCRIPT),
        *memory_materials(snapshot),
    )
    return REVIEW_MESSAGE_HEADER + "\n\n" + render_materials(materials)


class MemoryReviewScheduler:
    """周期与手动触发的后台记忆回顾：登记、执行与重启恢复，写库与调用模型都在本域。"""

    def __init__(
        self,
        *,
        memory_store: MemoryStore | None = None,
        path: Path | None = None,
        interval: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        # None 一律回退到当前配置；memory_store 缺省时按数据目录自建，与网关共享进程级锁。
        settings = get_settings()
        self.memory_store = memory_store or MemoryStore(settings.data_dir)
        self.path = path or settings.db_path
        self.interval = interval if interval is not None else settings.memory_review_interval
        self.enabled = enabled if enabled is not None else settings.memory_review_enabled

    def enqueue_if_due(self, task_id: str) -> dict | None:
        """所有任务自上次成功回顾后累计达到间隔个已完成用户轮时，登记全局回顾。"""
        if not self.enabled:
            return None
        now = timestamp()
        review_id = str(uuid4())
        with session(self.path) as conn, write(conn):
            if open_review(conn) is not None:
                return None
            from_rowid = last_through_sequence(conn)
            if done_message_count_since(conn, from_rowid) < self.interval:
                return None
            through_rowid = max_completed_sequence(conn)
            insert_review(conn, review_id, task_id, "interval", from_rowid, through_rowid, now)
            return review_response(review(conn, review_id))

    def enqueue_manual(self, task_id: str) -> dict:
        """手动登记全局回顾：覆盖上次成功回顾以来的窗口，不受间隔与自动开关限制。"""
        with session(self.path) as conn:
            repository.task(conn, task_id)
        now = timestamp()
        review_id = str(uuid4())
        with session(self.path) as conn, write(conn):
            existing = open_review(conn)
            if existing is not None:
                return review_response(existing)
            insert_review(
                conn,
                review_id,
                task_id,
                "manual",
                last_through_sequence(conn),
                max_completed_sequence(conn),
                now,
            )
            return review_response(review(conn, review_id))

    def pending_reviews(self) -> list[dict]:
        with session(self.path) as conn:
            return [review_response(row) for row in pending_reviews(conn)]

    def claim(self, review_id: str) -> dict | None:
        with session(self.path) as conn, write(conn):
            row = claim_review(conn, review_id, timestamp())
            return review_response(row) if row is not None else None

    def fail(self, review_id: str, message: str, *, failure: dict | None = None) -> None:
        with session(self.path) as conn, write(conn):
            finish_review(conn, review_id, "error", message, timestamp())
            if failure:
                conn.execute(
                    "UPDATE memory_reviews SET failure=? WHERE review_id=?",
                    (json.dumps(failure, ensure_ascii=False), review_id),
                )

    async def run(self, review_id: str, gateway: ReviewGateway) -> list[dict]:
        """执行一次回顾：取窗口对话与当前记忆，交给一次性模型调用，并落库结束状态。

        创建候选询问对话时，提示挂在锚点任务窗口内最后一轮上；返回写入的提示
        （`run_id`、`item_id`、`text`），由调用方推送给页面。
        """
        with session(self.path) as conn:
            row = review(conn, review_id)
            repository.task(conn, row["task_id"])
            items = window_text_items(conn, row["from_rowid"], row["through_rowid"])
        if not items:
            with session(self.path) as conn, write(conn):
                finish_review(conn, review_id, "done", None, timestamp())
            return []
        snapshot = self.memory_store.snapshot()
        message = build_review_message(render_transcript(items), snapshot)
        records = await gateway.review_memory(row["task_id"], REVIEW_INSTRUCTIONS, message)
        if records and records[-1].get("error"):
            raise MemoryValidationError(
                [{"field": "review", "message": "回顾的最后一项计划校验失败，未应用任何操作"}]
            )
        plans = [r["result"] for r in records if r.get("result", {}).get("planned")]
        applied_records = []
        if plans:
            operations = [op for plan in plans for op in plan["operations"]]
            versions = plans[0]["versions"]
            with self.memory_store._lock:
                snapshot = self.memory_store.snapshot()
                if any(plan["versions"] != versions for plan in plans) or any(
                    snapshot[t]["version"] != version for t, version in versions.items()
                ):
                    raise MemoryValidationError(
                        [{"field": "versions", "message": "回顾期间记忆变化，本次计划未应用"}]
                    )
                if any(op["action"] in {"replace", "delete", "move"} for op in operations):
                    result = MemoryProposals(self.memory_store, self.path).stage(
                        row["task_id"],
                        operations,
                        "；".join(dict.fromkeys(p["reason"] for p in plans if p["reason"])),
                    )
                else:
                    result = self.memory_store.edit(operations, expected_versions=versions)
                applied_records = [{"result": result}]
        texts = review_notice_texts(applied_records)
        published = []
        with session(self.path) as conn, write(conn):
            finish_review(conn, review_id, "done", None, timestamp())
            run_id = last_run_id(conn, row["task_id"], row["through_rowid"]) if texts else None
            if run_id is not None:
                for text in texts:
                    item_id = timeline.insert_notice(conn, row["task_id"], run_id, text)
                    published.append({"run_id": run_id, "item_id": item_id, "text": text})
        self.enqueue_if_due(row["task_id"])
        return published
