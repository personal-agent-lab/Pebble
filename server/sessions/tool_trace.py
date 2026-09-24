"""把一轮的工具尝试写进既有任务时间线；写入失败不改变工具执行。"""

from __future__ import annotations

import asyncio
import logging

from server.db import session, write
from server.sessions import timeline
from server.sessions.service import timestamp

logger = logging.getLogger(__name__)


def _begin(
    task_id: str, run_id: str, tool_call_id: str, name: str, arguments: dict, created_at: str
) -> None:
    try:
        with session() as conn, write(conn):
            timeline.start_tool_item(
                conn,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                name=name,
                arguments=arguments,
                created_at=created_at,
            )
    except Exception:
        logger.exception("工具 %s 的轨迹开始写入失败", name)


def _finish(
    task_id: str,
    run_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict,
    status: str,
    result: str,
) -> None:
    try:
        with session() as conn, write(conn):
            # 开始写入失败或未收到开始通知时，仍尽量留下实际结果。
            timeline.start_tool_item(
                conn,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                name=name,
                arguments=arguments,
                created_at=timestamp(),
            )
            timeline.finish_tool_item(
                conn,
                run_id=run_id,
                tool_call_id=tool_call_id,
                status=status,
                result=result,
            )
    except Exception:
        logger.exception("工具 %s 的轨迹结果写入失败", name)


async def begin_tool_call(
    *,
    task_id: str,
    run_id: str | None,
    tool_call_id: str,
    name: str,
    arguments: dict,
    created_at: str | None = None,
) -> None:
    if run_id is not None:
        await asyncio.to_thread(
            _begin, task_id, run_id, tool_call_id, name, arguments, created_at or timestamp()
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
) -> None:
    if run_id is not None:
        await asyncio.to_thread(
            _finish, task_id, run_id, tool_call_id, name, arguments, status, result
        )
