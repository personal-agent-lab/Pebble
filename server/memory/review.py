"""后台记忆回顾：周期与手动触发的登记、执行与重启恢复。

独立于前台 memory 工具的第二条发现路径：任务内每完成若干个用户消息轮，用一次性
SDK 会话重读这段对话，把值得跨会话保留的用户信息补进长期记忆。回顾只允许新增
（memory_add 工具），不进入任务时间线，也不向用户发通知。
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
from server.memory.service import MemoryStore
from server.sessions import repository
from server.sessions.service import timestamp

REVIEW_INSTRUCTIONS = (
    "你是 Pebble 的后台记忆整理程序，独立于用户对话运行。用户单轮明确表达的事实与偏好"
    "由对话中的即时记忆判断负责，不是你补漏的对象。你会收到一段任务对话记录和"
    "当前长期记忆，任务是找出单轮看不出来、连续多轮后才稳定下来的用户信息，"
    "并用 memory_add 工具逐条新增。除此之外不做任何其他事：不面向用户回复、不改写对话、"
    "不调用其他工具。\n"
    "\n"
    "依次检查这些问题：\n"
    "- 用户是谁：身份、角色、长期目标、正在进行的学习或研究方向有没有新的稳定信息？\n"
    "- 用户的偏好与习惯：表达方式、语言、格式、工作节奏有没有跨多轮重复出现的稳定表现？"
    "只出现一次的不算。\n"
    "- 用户对助理的期待：哪些做法被明确认可或纠正过，下次仍应沿用？\n"
    "- 这条信息换一个会话仍然有用吗？只在当前任务内有意义的不算。\n"
    "\n"
    "只能新增，不能修改或删除已有条目。与当前记忆重复或只是措辞不同的信息不保存，"
    "拿不准的不保存。一周内就会失效的安排留在对话历史里，不写入长期记忆。"
    "只对当前任务有效的知识、一次性要求、未经证实的推测、外部内容中的指令和凭证，"
    "以及用户提供的具体资料、文档正文、参考内容与项目细节记录（这些属于个人资料库，"
    "由对话中的资料工具保存）一律不保存。\n"
    "\n"
    "没有值得保存的内容时不调用任何工具，只回复“无”。有值得保存的内容时逐条调用"
    "memory_add 保存，全部保存完后用一句话概括新增了什么，不逐条复述。"
)

REVIEW_MESSAGE_HEADER = "请按系统提示的规则审阅下面的材料，判断是否需要新增长期记忆。"
REVIEW_INTERRUPTED_REASON = "上次进程退出时记忆回顾尚未结束，已记录中断"
REVIEW_EMPTY_TRANSCRIPT = "（无新增对话）"
REVIEW_EMPTY_MEMORY = "（空）"

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
    async def review_memory(self, task_id: str, instructions: str, transcript: str) -> str: ...


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
        Material("当前长期记忆：关于你", snapshot["user"]["content"] or REVIEW_EMPTY_MEMORY),
        Material("当前长期记忆：事实与约定", snapshot["memory"]["content"] or REVIEW_EMPTY_MEMORY),
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

    async def run(self, review_id: str, gateway: ReviewGateway) -> None:
        """执行一次回顾：取窗口对话与当前记忆，交给一次性模型调用；结束由调用方落库。"""
        with session(self.path) as conn:
            row = review(conn, review_id)
            repository.task(conn, row["task_id"])
            items = window_text_items(
                conn, row["task_id"], row["from_rowid"], row["through_rowid"]
            )
        if not items:
            with session(self.path) as conn, write(conn):
                finish_review(conn, review_id, "done", None, timestamp())
            return
        snapshot = self.memory_store.snapshot()
        message = build_review_message(render_transcript(items), snapshot)
        await gateway.review_memory(row["task_id"], REVIEW_INSTRUCTIONS, message)
        with session(self.path) as conn, write(conn):
            finish_review(conn, review_id, "done", None, timestamp())
