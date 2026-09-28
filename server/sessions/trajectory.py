"""执行轨迹视图（docs/observability.md §2）：时间线与轮次记录的可查询形状。

轨迹不新建存储：条目来自 task_timeline_items（按 sequence 排序），轮次来自
agent_runs。这里的职责是把两者拼成契约 §3 的形状——轮状态映射、条目角色映射、
按条目边界的截取——供后台复盘装配材料与依据条目校验使用，不接 HTTP。
"""

from __future__ import annotations

import json
import sqlite3

from server.errors import NotFoundError

# agent_runs 的内部状态 → 轨迹视图的轮状态（契约 §3）。
TURN_STATUS = {
    "pending": "running",
    "running": "running",
    "done": "completed",
    "error": "failed",
    "interrupted": "cancelled",
}


# 条目角色：契约 §3 只认 user/assistant/notice/tool；错误与草稿归入 notice 一侧，
# 原始 kind 一并带出，复盘装配按需取用。
def _role(kind: str, role: str | None) -> str:
    if kind == "text":
        return role or "user"
    if kind == "tool":
        return "tool"
    return "notice"


def _sequence_of(conn: sqlite3.Connection, task_id: str, item_id: str) -> int:
    """边界条目的序号；不存在或不属于该任务时按契约按未知条目处理。"""
    row = conn.execute(
        "SELECT task_id, sequence FROM task_timeline_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None or row["task_id"] != task_id:
        raise NotFoundError(item_id)
    return int(row["sequence"])


def trajectory(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    since_item_id: str | None = None,
    through_item_id: str | None = None,
) -> dict:
    """一个任务的有序轨迹：轮次（含状态）+ 全部条目（契约 §3 形状）。

    since_item_id 为排他下界（上次复盘或上次写入的边界），through_item_id 为
    含入上界（本次复盘的输入边界）；都按条目序号截取，不传则不设该侧边界。
    """
    clauses = ["i.task_id = ?"]
    params: list = [task_id]
    if since_item_id is not None:
        clauses.append("i.sequence > ?")
        params.append(_sequence_of(conn, task_id, since_item_id))
    if through_item_id is not None:
        clauses.append("i.sequence <= ?")
        params.append(_sequence_of(conn, task_id, through_item_id))
    rows = conn.execute(
        "SELECT i.*, r.status AS run_status, r.started_at, r.finished_at "
        "FROM task_timeline_items i JOIN agent_runs r ON r.run_id = i.run_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY i.sequence",
        params,
    ).fetchall()

    turns: dict[str, dict] = {}
    entries = []
    for row in rows:
        item = dict(row)
        run_id = item["run_id"]
        if run_id not in turns:
            turns[run_id] = {
                "turn_id": run_id,
                "status": TURN_STATUS.get(item["run_status"], "running"),
                "started_at": item["started_at"],
                "ended_at": item["finished_at"],
            }
        entry = {
            "item_id": item["item_id"],
            "turn_id": run_id,
            "sequence": item["sequence"],
            "kind": item["kind"],
            "role": _role(item["kind"], item["role"]),
            "content": item["tool_result"] if item["kind"] == "tool" else item["text"],
            "created_at": item["created_at"],
        }
        if item["kind"] == "tool":
            entry |= {
                "tool_call_id": item["tool_call_id"],
                "name": item["tool_name"],
                "arguments": json.loads(item["tool_arguments"]),
                "status": item["tool_status"],
            }
        if item["kind"] == "mail_draft":
            entry["operation_id"] = item["operation_id"]
        entries.append(entry)
    return {
        "task_id": task_id,
        "turns": list(turns.values()),
        "entries": entries,
    }


def item_within_boundary(
    conn: sqlite3.Connection, task_id: str, item_id: str, through_item_id: str
) -> bool:
    """依据条目是否存在且不晚于边界条目；复盘沉淀时校验 evidence_item_ids 用。"""
    row = conn.execute(
        "SELECT task_id, sequence FROM task_timeline_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None or row["task_id"] != task_id:
        return False
    boundary = conn.execute(
        "SELECT sequence FROM task_timeline_items WHERE item_id = ? AND task_id = ?",
        (through_item_id, task_id),
    ).fetchone()
    return boundary is not None and row["sequence"] <= boundary["sequence"]
