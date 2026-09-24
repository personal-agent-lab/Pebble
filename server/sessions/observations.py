"""运行观测：一轮的运行摘要与工具、压缩、降级步骤的写入和读取。

写入失败只进进程日志：观测绝不影响对话、工具执行与外部写入，也不递归记录自身的
失败。工具参数与返回正文只在时间线保存一份，这里只存引用、状态与规模；读取面把
缺失字段原样返回空值，由界面显示「未记录」。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from server.db import session, write
from server.sessions.service import timestamp

logger = logging.getLogger(__name__)


def _dump(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


def _load(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


# ---------- 摘要：嵌入调用方事务的字段级写入 ----------


def upsert_summary(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    materials: dict | None = None,
    sdk_result: dict | None = None,
    context_before: dict | None = None,
    context_after: dict | None = None,
) -> None:
    """按字段更新一轮摘要；未提供的字段保持原值。字段只会从空到有，不会清空。"""
    conn.execute(
        "INSERT INTO run_observations (run_id, materials, sdk_result, context_before, "
        "context_after, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "materials = COALESCE(excluded.materials, run_observations.materials), "
        "sdk_result = COALESCE(excluded.sdk_result, run_observations.sdk_result), "
        "context_before = COALESCE(excluded.context_before, run_observations.context_before), "
        "context_after = COALESCE(excluded.context_after, run_observations.context_after), "
        "updated_at = excluded.updated_at",
        (
            run_id,
            _dump(materials),
            _dump(sdk_result),
            _dump(context_before),
            _dump(context_after),
            timestamp(),
        ),
    )


def fill_compact_after(conn: sqlite3.Connection, run_id: str, after: dict) -> None:
    """轮末读数回填本轮压缩步骤缺失的后占用。"""
    rows = conn.execute(
        "SELECT step_id, detail FROM observation_steps WHERE run_id = ? AND kind = 'compact'",
        (run_id,),
    ).fetchall()
    for row in rows:
        detail = _load(row["detail"])
        if not isinstance(detail, dict) or detail.get("after") is not None:
            continue
        detail["after"] = after
        conn.execute(
            "UPDATE observation_steps SET detail = ? WHERE step_id = ?",
            (_dump(detail), row["step_id"]),
        )


# ---------- 工具步骤：与时间线工具条目同事务 ----------


def begin_tool_step(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    tool_call_id: str,
    name: str,
    source: str,
    started_at: str,
    item_id: str | None = None,
) -> None:
    """调用开始即落 running 步骤；同一调用重复开始通知不生成第二条。"""
    conn.execute(
        "INSERT INTO observation_steps (step_id, run_id, kind, code, status, started_at, "
        "ended_at, item_id, tool_call_id, detail) "
        "VALUES (?, ?, 'tool', ?, 'running', ?, NULL, ?, ?, ?) "
        "ON CONFLICT(run_id, tool_call_id) WHERE kind='tool' DO NOTHING",
        (str(uuid4()), run_id, name, started_at, item_id, tool_call_id, _dump({"source": source})),
    )


def finish_tool_step(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    tool_call_id: str,
    status: str,
    ended_at: str,
    result_chars: int | None = None,
) -> bool:
    """只完成仍在运行的步骤；重复的成功、失败或拒绝通知不覆盖首次结果。"""
    row = conn.execute(
        "SELECT detail FROM observation_steps "
        "WHERE run_id = ? AND tool_call_id = ? AND kind = 'tool'",
        (run_id, tool_call_id),
    ).fetchone()
    if row is None:
        return False
    detail = _load(row["detail"])
    detail = detail if isinstance(detail, dict) else {}
    if result_chars is not None:
        detail["chars"] = result_chars
    changed = conn.execute(
        "UPDATE observation_steps SET status = ?, ended_at = ?, detail = ? "
        "WHERE run_id = ? AND tool_call_id = ? AND kind = 'tool' AND status = 'running'",
        (status, ended_at, _dump(detail), run_id, tool_call_id),
    )
    return changed.rowcount == 1


def complete_tool_step(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    tool_call_id: str,
    name: str,
    source: str,
    status: str,
    ended_at: str,
    started_at: str | None = None,
    result_chars: int | None = None,
    item_id: str | None = None,
) -> None:
    """写入终态：开始行已存在则完成它；缺失（权限拒绝、开始写入失败）时直接落终态。

    直接落终态时 started_at 为空——拒绝没有真实的开始信号，界面显示「未记录」。
    """
    if finish_tool_step(
        conn,
        run_id=run_id,
        tool_call_id=tool_call_id,
        status=status,
        ended_at=ended_at,
        result_chars=result_chars,
    ):
        return
    detail: dict[str, Any] = {"source": source}
    if result_chars is not None:
        detail["chars"] = result_chars
    conn.execute(
        "INSERT INTO observation_steps (step_id, run_id, kind, code, status, started_at, "
        "ended_at, item_id, tool_call_id, detail) "
        "VALUES (?, ?, 'tool', ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, tool_call_id) WHERE kind='tool' DO NOTHING",
        (
            str(uuid4()),
            run_id,
            name,
            status,
            started_at,
            ended_at,
            item_id,
            tool_call_id,
            _dump(detail),
        ),
    )


# ---------- 独立连接的步骤写入（降级与压缩） ----------


def record_degraded_sync(run_id: str, code: str, detail: dict | None = None) -> None:
    """记一条关键静默降级；观测自身失败只进日志，不递归记步骤。"""
    try:
        with session() as conn, write(conn):
            now = timestamp()
            conn.execute(
                "INSERT INTO observation_steps (step_id, run_id, kind, code, status, started_at, "
                "ended_at, item_id, tool_call_id, detail) "
                "VALUES (?, ?, 'degraded', ?, 'ok', ?, ?, NULL, NULL, ?)",
                (str(uuid4()), run_id, code, now, now, _dump(detail)),
            )
    except Exception:
        logger.exception("降级观测步骤写入失败")


# ---------- 网关侧的一轮记录器 ----------


def _field(source: Any, key: str) -> Any:
    """SDK 的 usage 是 TypedDict（运行时为 dict），消息本体是数据类；两者都要能读。"""
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


class TurnObserver:
    """一轮的观测记录器：内存累计请求级用量，按事件分字段写入摘要。

    方法全部同步：沿用本仓库在调用路径上直接写 SQLite 的惯例，写入量小且
    失败只进日志。观测绝不阻塞或改变对话与工具执行。
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._usage: list[dict] = []
        self._identified: set[str] = set()
        self._skipped: list[dict] = []

    def collect_usage(self, message: Any) -> None:
        """逐次读取 AssistantMessage 的请求级用量；按 message_id 或 request_id 去重。"""
        usage = getattr(message, "usage", None)
        message_id = getattr(message, "message_id", None)
        request_id = _field(usage, "request_id") if usage is not None else None
        key = (
            message_id
            if isinstance(message_id, str)
            else (request_id if isinstance(request_id, str) else None)
        )
        if key is not None:
            if key in self._identified:
                return
            self._identified.add(key)
        elif usage is None:
            # 既无标识也无用量：无从记录也无从去重，不产生条目。
            return
        self._usage.append(
            {
                "message_id": message_id if isinstance(message_id, str) else None,
                "request_id": request_id if isinstance(request_id, str) else None,
                "input_tokens": _field(usage, "input_tokens"),
                "output_tokens": _field(usage, "output_tokens"),
                "credits": _field(usage, "credits"),
            }
        )

    def degraded(
        self, code: str, detail: dict | None = None, *, skipped: dict | None = None
    ) -> None:
        """记一条关键静默降级；属于材料跳过时同步进摘要的跳过清单。"""
        record_degraded_sync(self.run_id, code, detail)
        if skipped is not None:
            self._skipped.append(skipped)

    def record_materials(self, assembled: list[dict]) -> None:
        self._write(materials={"assembled": assembled, "skipped": list(self._skipped)})

    def record_result(self, message: Any) -> None:
        self._write(
            sdk_result={
                "duration_ms": getattr(message, "duration_ms", None),
                "duration_api_ms": getattr(message, "duration_api_ms", None),
                "num_turns": getattr(message, "num_turns", None),
                "is_error": bool(getattr(message, "is_error", False)),
                "usage": list(self._usage),
            }
        )

    def record_context(self, *, before: dict | None = None, after: dict | None = None) -> None:
        def action(conn: sqlite3.Connection) -> None:
            upsert_summary(conn, self.run_id, context_before=before, context_after=after)
            if after is not None:
                fill_compact_after(conn, self.run_id, after)

        self._run(action)

    def record_compact(self, *, auto: bool, before: dict | None, after: dict | None = None) -> None:
        """压缩只在观察到实际边界信号后记录。"""
        now = timestamp()
        detail = {"auto": auto, "before": before, "after": after}

        def action(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO observation_steps (step_id, run_id, kind, code, status, started_at, "
                "ended_at, item_id, tool_call_id, detail) "
                "VALUES (?, ?, 'compact', 'compact', 'ok', ?, ?, NULL, NULL, ?)",
                (str(uuid4()), self.run_id, now, now, _dump(detail)),
            )

        self._run(action)

    def _write(self, **fields: dict | None) -> None:
        self._run(lambda conn: upsert_summary(conn, self.run_id, **fields))

    def _run(self, action: Callable[[sqlite3.Connection], None]) -> None:
        try:
            with session() as conn, write(conn):
                action(conn)
        except Exception:
            logger.exception("轮次 %s 的观测写入失败", self.run_id)


# ---------- 读取 ----------


def usage_totals(entries: list[dict] | None) -> dict:
    """只对可唯一识别且字段齐全的请求求和；否则对应合计为空值（界面显示「未记录」）。"""
    keys = ("input_tokens", "output_tokens", "credits")
    empty = {key: None for key in keys}
    if not entries:
        return empty
    totals = {key: 0 for key in keys}
    for entry in entries:
        if not (entry.get("message_id") or entry.get("request_id")):
            return empty
        for key in keys:
            value = entry.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return empty
            totals[key] += value
    return totals


def read_observations(task_id: str, path: Path | None = None) -> list[dict]:
    """按 agent_runs 顺序返回轮次摘要与步骤；无观测数据的轮次各字段为空值。"""
    with session(path) as conn:
        runs = conn.execute(
            "SELECT r.run_id, r.kind, r.status, r.created_at, r.started_at, r.finished_at, "
            "t.model, o.materials, o.sdk_result, o.context_before, o.context_after "
            "FROM agent_runs r JOIN tasks t ON t.task_id = r.task_id "
            "LEFT JOIN run_observations o ON o.run_id = r.run_id "
            "WHERE r.task_id = ? ORDER BY r.rowid",
            (task_id,),
        ).fetchall()
        steps: dict[str, list[dict]] = {}
        for row in conn.execute(
            "SELECT s.step_id, s.run_id, s.kind, s.code, s.status, s.started_at, s.ended_at, "
            "s.item_id, s.tool_call_id, s.detail FROM observation_steps s "
            "JOIN agent_runs r ON r.run_id = s.run_id WHERE r.task_id = ? ORDER BY s.rowid",
            (task_id,),
        ):
            steps.setdefault(row["run_id"], []).append(
                {
                    "step_id": row["step_id"],
                    "kind": row["kind"],
                    "code": row["code"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "item_id": row["item_id"],
                    "tool_call_id": row["tool_call_id"],
                    "detail": _load(row["detail"]),
                }
            )
    observations = []
    for row in runs:
        sdk_result = _load(row["sdk_result"])
        usage = sdk_result.get("usage") if isinstance(sdk_result, dict) else None
        observations.append(
            {
                "run_id": row["run_id"],
                "kind": row["kind"],
                "status": row["status"],
                "model": row["model"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "materials": _load(row["materials"]),
                "sdk_result": sdk_result,
                "usage_totals": usage_totals(usage if isinstance(usage, list) else None),
                "context_before": _load(row["context_before"]),
                "context_after": _load(row["context_after"]),
                "steps": steps.get(row["run_id"], []),
            }
        )
    return observations
