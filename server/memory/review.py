"""后台记忆回顾：周期与手动触发的登记、执行与重启恢复。

独立于每轮判断的第二条路径：任务内每完成若干个用户消息轮，用一次性 SDK 会话重读这段
对话，补进跨轮才稳定下来的用户信息，并整理长期记忆（合并重复、更新过时、删除失效）。
回顾不占用对话轮；新增不通知，修改与删除以程序提示挂在窗口内最后一轮上。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from server.agent.context import Material, render_materials
from server.config import get_settings
from server.db import session, write
from server.errors import NotFoundError
from server.memory.notices import (
    MEMORY_CONSOLIDATE_LIMITS,
    MEMORY_NOT_SAVE,
    MEMORY_WRITE_STYLE,
    memory_materials,
    review_notice_texts,
)
from server.memory.service import MemoryStore
from server.sessions import repository, timeline
from server.sessions.service import timestamp

REVIEW_INSTRUCTIONS = (
    "你是 Pebble 的后台记忆整理程序，独立于用户对话运行。用户单轮明确表达的事实与偏好"
    "由对话中的即时记忆判断负责，不是你补漏的对象。你会收到一段任务对话记录和"
    "当前长期记忆，要做两件事：补充跨轮才稳定下来的用户信息，并整理长期记忆。"
    "除此之外不做任何其他事：不面向用户回复、不改写对话。\n"
    "\n"
    "补充（memory_edit 追加，或并入相关段落），依次检查：\n"
    "- 用户是谁：身份、角色、长期目标、正在进行的学习或研究方向有没有新的稳定信息？\n"
    "- 用户的偏好与习惯：表达方式、语言、格式、工作节奏有没有跨多轮重复出现的稳定表现？"
    "只出现一次的不算。\n"
    "- 用户对助理的期待：哪些做法被明确认可或纠正过，下次仍应沿用？\n"
    "- 这条信息换一个会话仍然有用吗？只在当前任务内有意义的不算。\n"
    f"{MEMORY_WRITE_STYLE}\n"
    f"与当前记忆重复或只是措辞不同、拿不准的不保存。{MEMORY_NOT_SAVE}\n"
    "\n"
    "整理（memory_edit 修改或删除原文）：\n"
    "- 每块记忆是一份 Markdown 文档：把相关内容归到一起，合并重复或含义相近的内容，"
    "精简冗长的措辞。\n"
    "- 对话里有明确依据表明某项已经过时或失效时，更新或删除它；"
    "没有明确依据时不改动已有内容。\n"
    "- 容量接近上限（材料标题里有已用与上限字数）时优先整理，为新信息腾出空间；"
    "整理与新增放在同一次带 operations 的调用里。\n"
    "- 放错分区的内容移到正确分区：在两个分区各调用一次（一处删除、一处追加）。\n"
    f"- {MEMORY_CONSOLIDATE_LIMITS}\n"
    "\n"
    "没有需要做的事时不调用任何工具，只回复“无”。完成后用一句话概括做了什么，不逐条复述。"
)

REVIEW_MESSAGE_HEADER = "请按系统提示的规则审阅下面的材料，判断是否需要补充或整理长期记忆。"
REVIEW_INTERRUPTED_REASON = "上次进程退出时记忆回顾尚未结束，已记录中断"
REVIEW_EMPTY_TRANSCRIPT = "（无新增对话）"

REVIEW_FIELDS = (
    "review_id",
    "task_id",
    "status",
    "origin",
    "from_rowid",
    "through_rowid",
    "error",
    "created_at",
    "started_at",
    "finished_at",
)

INSERT_SQL = (
    "INSERT INTO memory_reviews "
    "(review_id, task_id, status, origin, from_rowid, through_rowid, created_at) "
    "VALUES (?, ?, 'pending', ?, ?, ?, ?)"
)


class ReviewGateway(Protocol):
    """一次性记忆回顾调用：返回按顺序记录的工具调用与结果。"""

    async def review_memory(
        self, task_id: str, instructions: str, transcript: str
    ) -> list[dict]: ...


def review_response(row: dict) -> dict:
    return {field: row[field] for field in REVIEW_FIELDS}


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
            "SELECT * FROM memory_reviews WHERE status = 'pending' ORDER BY rowid"
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
    # 行数可为 0：回顾执行中任务可能已被删除，落库结果随之清理。
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


def open_review(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM memory_reviews WHERE task_id = ? AND status IN ('pending','running') "
        "ORDER BY rowid LIMIT 1",
        (task_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def last_through_rowid(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute(
        "SELECT through_rowid FROM memory_reviews WHERE task_id = ? AND status = 'done' "
        "ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["through_rowid"] if row is not None else 0


def done_message_count_since(conn: sqlite3.Connection, task_id: str, through_rowid: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM agent_runs WHERE task_id = ? AND kind = 'message' "
        "AND status = 'done' AND rowid > ?",
        (task_id, through_rowid),
    ).fetchone()["n"]


def max_done_message_rowid(conn: sqlite3.Connection, task_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) AS m FROM agent_runs "
        "WHERE task_id = ? AND kind = 'message' AND status = 'done'",
        (task_id,),
    ).fetchone()["m"]


def max_run_rowid(conn: sqlite3.Connection, task_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) AS m FROM agent_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()["m"]


def last_run_id(conn: sqlite3.Connection, task_id: str, through_rowid: int) -> str | None:
    """窗口内最后一个调用；任务在回顾期间被删除时没有结果，提示随之不写。"""
    row = conn.execute(
        "SELECT run_id FROM agent_runs WHERE task_id = ? AND rowid <= ? "
        "ORDER BY rowid DESC LIMIT 1",
        (task_id, through_rowid),
    ).fetchone()
    return row["run_id"] if row is not None else None


def window_text_items(
    conn: sqlite3.Connection, task_id: str, from_rowid: int, through_rowid: int
) -> list[dict]:
    """回顾窗口内的对话文本：只取窗口内已结束调用的 text 与 notice 项。

    notice 是程序生成的记忆提示：即时判断的保存结果与追问属于回顾要参考的上下文。
    草稿卡片与错误项不算对话。
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT i.kind, i.role, i.text FROM task_timeline_items i "
            "JOIN agent_runs r ON r.run_id = i.run_id "
            "WHERE i.task_id = ? AND i.kind IN ('text', 'notice') "
            "AND r.rowid > ? AND r.rowid <= ? ORDER BY i.rowid",
            (task_id, from_rowid, through_rowid),
        )
    ]


def render_transcript(items: list[dict]) -> str:
    labels = {"user": "用户", "assistant": "助手"}
    return "\n".join(
        f"{'系统' if item.get('kind') == 'notice' else labels.get(item['role'], item['role'])}"
        f"：{item['text']}"
        for item in items
    )


def build_review_message(transcript: str, snapshot: dict) -> str:
    materials = (
        Material("自上次回顾以来的任务对话", transcript or REVIEW_EMPTY_TRANSCRIPT),
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
        """任务内自上次已完成回顾后又攒够间隔个已完成的用户消息轮时，登记一次回顾。"""
        if not self.enabled:
            return None
        now = timestamp()
        review_id = str(uuid4())
        with session(self.path) as conn, write(conn):
            if open_review(conn, task_id) is not None:
                return None
            from_rowid = last_through_rowid(conn, task_id)
            if done_message_count_since(conn, task_id, from_rowid) < self.interval:
                return None
            through_rowid = max_done_message_rowid(conn, task_id)
            insert_review(conn, review_id, task_id, "interval", from_rowid, through_rowid, now)
            return review_response(review(conn, review_id))

    def enqueue_manual(self, task_id: str) -> dict:
        """手动登记一次回顾：覆盖整个任务，不受间隔与自动开关限制。"""
        with session(self.path) as conn:
            repository.task(conn, task_id)
        now = timestamp()
        review_id = str(uuid4())
        with session(self.path) as conn, write(conn):
            existing = open_review(conn, task_id)
            if existing is not None:
                return review_response(existing)
            insert_review(conn, review_id, task_id, "manual", 0, max_run_rowid(conn, task_id), now)
            return review_response(review(conn, review_id))

    def pending_reviews(self) -> list[dict]:
        with session(self.path) as conn:
            return [review_response(row) for row in pending_reviews(conn)]

    def claim(self, review_id: str) -> dict | None:
        with session(self.path) as conn, write(conn):
            row = claim_review(conn, review_id, timestamp())
            return review_response(row) if row is not None else None

    def fail(self, review_id: str, message: str) -> None:
        with session(self.path) as conn, write(conn):
            finish_review(conn, review_id, "error", message, timestamp())

    async def run(self, review_id: str, gateway: ReviewGateway) -> list[dict]:
        """执行一次回顾：取窗口对话与当前记忆，交给一次性模型调用，并落库结束状态。

        整理改动了已有内容时，提示挂在窗口内最后一轮上写入时间线；返回写入的提示
        （`run_id`、`item_id`、`text`），由调用方推送给页面。
        """
        with session(self.path) as conn:
            row = review(conn, review_id)
            repository.task(conn, row["task_id"])
            items = window_text_items(conn, row["task_id"], row["from_rowid"], row["through_rowid"])
        if not items:
            with session(self.path) as conn, write(conn):
                finish_review(conn, review_id, "done", None, timestamp())
            return []
        snapshot = self.memory_store.snapshot()
        message = build_review_message(render_transcript(items), snapshot)
        records = await gateway.review_memory(row["task_id"], REVIEW_INSTRUCTIONS, message)
        texts = review_notice_texts(records)
        published = []
        with session(self.path) as conn, write(conn):
            finish_review(conn, review_id, "done", None, timestamp())
            run_id = last_run_id(conn, row["task_id"], row["through_rowid"]) if texts else None
            if run_id is not None:
                for text in texts:
                    item_id = timeline.insert_notice(conn, row["task_id"], run_id, text)
                    published.append({"run_id": run_id, "item_id": item_id, "text": text})
        return published
