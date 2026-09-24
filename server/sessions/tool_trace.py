"""把一轮的工具尝试写进既有任务时间线；写入失败不改变工具执行。

同一事务里同步写观测步骤：与时间线工具条目共用 item_id，工具参数与返回
正文仍只在时间线保存一份，观测只记引用、状态与规模。
"""

from __future__ import annotations

import asyncio
import logging

from server.db import session, write
from server.sessions import observations, timeline
from server.sessions.service import timestamp

logger = logging.getLogger(__name__)


def _begin(
    task_id: str,
    run_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict,
    created_at: str,
    source: str,
) -> None:
    try:
        with session() as conn, write(conn):
            item_id = timeline.start_tool_item(
                conn,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                name=name,
                arguments=arguments,
                created_at=created_at,
            )
            observations.begin_tool_step(
                conn,
                run_id=run_id,
                tool_call_id=tool_call_id,
                name=name,
                source=source,
                started_at=created_at,
                item_id=item_id,
            )
    except Exception:
        logger.exception("工具 %s 的轨迹开始写入失败", name)
        observations.record_degraded_sync(
            run_id, "tool_trace_write_failed", {"tool": name, "phase": "begin"}
        )


def _finish(
    task_id: str,
    run_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict,
    status: str,
    result: str,
    source: str,
    denied: bool,
) -> None:
    now = timestamp()
    try:
        with session() as conn, write(conn):
            # 开始写入失败或未收到开始通知时，仍尽量留下实际结果。
            item_id = timeline.start_tool_item(
                conn,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                name=name,
                arguments=arguments,
                created_at=now,
            )
            timeline.finish_tool_item(
                conn,
                run_id=run_id,
                tool_call_id=tool_call_id,
                status=status,
                result=result,
            )
            observations.complete_tool_step(
                conn,
                run_id=run_id,
                tool_call_id=tool_call_id,
                name=name,
                source=source,
                status="denied" if denied else status,
                ended_at=now,
                result_chars=len(result),
                item_id=item_id,
            )
    except Exception:
        logger.exception("工具 %s 的轨迹结果写入失败", name)
        observations.record_degraded_sync(
            run_id, "tool_trace_write_failed", {"tool": name, "phase": "finish"}
        )


async def begin_tool_call(
    *,
    task_id: str,
    run_id: str | None,
    tool_call_id: str,
    name: str,
    arguments: dict,
    created_at: str | None = None,
    source: str = "mcp",
) -> None:
    if run_id is not None:
        await asyncio.to_thread(
            _begin,
            task_id,
            run_id,
            tool_call_id,
            name,
            arguments,
            created_at or timestamp(),
            source,
        )


async def finish_tool_call(
    *,
    task_id: str,
    run_id: str | None,
    tool_call_id: str,
    name: str,
    arguments: dict,
    status: str,
    result: str,
    source: str = "mcp",
    denied: bool = False,
) -> None:
    if run_id is not None:
        await asyncio.to_thread(
            _finish,
            task_id,
            run_id,
            tool_call_id,
            name,
            arguments,
            status,
            result,
            source,
            denied,
        )
