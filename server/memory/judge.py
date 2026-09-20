"""每轮专用记忆判断：即时路径的提示词与输入组装。

每个用户消息轮由独立的一次性模型调用判断长期记忆是否需要变化（新增、替换、
删除或无变化），与主回答并行；主 Agent 不承担隐式记忆识别。判断只通过
judge_registry 的 memory_edit 表达，歧义内容保持不变，整个过程不进入对话。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Protocol

from server.agent.context import Material, render_materials
from server.db import session
from server.memory.notices import MEMORY_RULES, memory_materials
from server.memory.review import render_transcript
from server.memory.service import MemoryStore

logger = logging.getLogger(__name__)

JUDGE_INSTRUCTIONS = (
    "你是 Pebble 的长期记忆判断程序，独立于用户对话运行，只通过工具表达判断，不回答消息内容。"
    "你会收到用户刚发的消息、近期对话和当前长期记忆，判断这条消息是否应当改变长期记忆。\n"
    "\n"
    "- 只保存用户明确表达的长期信息：身份、长期目标、持续的学习或研究方向、稳定偏好、"
    "对助理的持续要求与纠正。从提问方式推断出的偏好不保存，由后台回顾负责。\n"
    "- 结合近期对话判断适用范围；任务进行中补充的地点、时间、措辞等默认只服务当前任务。"
    "例如起草邮件时说“我在南京”只用于邮件，不保存为常住地。\n"
    "- 无变化：不调用工具，只回复“无”。\n"
    "- 新增、修改、要求忘记：调用 memory_edit，一轮只做必要的改动。\n"
    "- 拿不准是替换还是并存，或拿不准用户是否指长期信息：不调用工具，不写入，也不向用户提问。\n"
    "\n"
    f"{MEMORY_RULES}"
)

JUDGE_MESSAGE_HEADER = "请按系统提示的规则判断下面的材料是否需要改变长期记忆。"
JUDGE_EMPTY_CONTEXT = "（无此前对话）"

# 近期对话窗口：最近若干个已完成的用户消息轮。
JUDGE_RECENT_TURNS = 5


def recent_text_items(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """判断输入的近期对话：最近若干个已完成用户消息轮以来的 text 与 notice 项，正序。

    程序提示（notice）一并进入：判断需要看到自己此前的追问与保存结果提示。
    当前轮仍在进行（status 非 done），天然不在结果里；刚发的消息由调用方单独传入。
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT i.kind, i.role, i.text FROM task_timeline_items i "
            "JOIN agent_runs r ON r.run_id = i.run_id "
            "WHERE i.task_id = ? AND i.kind IN ('text', 'notice') AND r.status = 'done' "
            "AND r.rowid >= COALESCE(("
            "SELECT MIN(rowid) FROM ("
            "SELECT rowid FROM agent_runs "
            "WHERE task_id = ? AND kind = 'message' AND status = 'done' "
            "ORDER BY rowid DESC LIMIT ?)), 0) "
            "ORDER BY i.rowid",
            (task_id, task_id, JUDGE_RECENT_TURNS),
        )
    ]


def build_judge_message(message: str, transcript: str, snapshot: dict) -> str:
    materials = (
        Material("用户刚发的消息", message),
        Material("近期对话", transcript or JUDGE_EMPTY_CONTEXT),
        *memory_materials(snapshot),
    )
    return JUDGE_MESSAGE_HEADER + "\n\n" + render_materials(materials)


class JudgeGateway(Protocol):
    """一次性记忆判断调用：返回按顺序记录的工具调用与结果。"""

    async def judge_memory(self, task_id: str, instructions: str, message: str) -> list[dict]: ...


async def run_judgment(
    gateway: JudgeGateway, store: MemoryStore, path: Path, *, task_id: str, message: str
) -> None:
    """执行一轮静默记忆判断：组装输入并调用一次性判断会话。"""
    with session(path) as conn:
        transcript = render_transcript(recent_text_items(conn, task_id))
    snapshot = store.snapshot()
    prompt = build_judge_message(message, transcript, snapshot)
    records = await gateway.judge_memory(task_id, JUDGE_INSTRUCTIONS, prompt)
    for record in records:
        if error := record.get("error"):
            logger.warning(
                "记忆判断工具失败：%s：%s",
                error.get("error", "unknown"),
                error.get("message", "未知错误"),
            )
