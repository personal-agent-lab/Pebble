"""边界故障的安全描述；恢复建议不授予重试或外部执行权限。"""

from __future__ import annotations

import logging
import sqlite3
import traceback
from dataclasses import asdict, dataclass, field, replace
from typing import Literal
from uuid import uuid4

from server.errors import error_details

Recovery = Literal[
    "none",
    "check_configuration",
    "correct_input",
    "read_current",
    "wait",
    "verify_result",
    "new_session",
]
Impact = Literal["request", "tool", "turn", "degraded", "unknown"]


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    source: str
    stage: str
    impact: Impact
    recovery: Recovery = "none"
    diagnostic_id: str = field(default_factory=lambda: uuid4().hex)
    # 仅由边界显式提供安全的协议元数据，绝不放异常正文、凭证或请求内容。
    details: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return asdict(self)


class FailureError(Exception):
    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.message)


def exception_failure(
    error: Exception, *, source: str, stage: str, impact: Impact = "request"
) -> Failure:
    if isinstance(error, FailureError):
        return replace(error.failure, impact=impact)
    if isinstance(error, ExceptionGroup):
        failures = [
            exception_failure(child, source=source, stage=stage, impact=impact)
            for child in error.exceptions
            if isinstance(child, Exception)
        ]
        if len(failures) == 1:
            return failures[0]
    business = error_details(error)
    if business is not None:
        code = business["error"]
        recovery: Recovery = "correct_input"
        if code in {"unavailable", "invalid_model"}:
            recovery = "check_configuration"
        elif code in {"version_conflict", "skill_conflict", "not_found", "unknown_skill"}:
            recovery = "read_current"
        # 服务/文件不可用错误可能包含来自适配器的原始异常，使用固定文案。
        message = business["message"]
        if code.endswith("_unavailable"):
            message = {
                "memory_store_unavailable": "长期记忆存储当前不可用",
                "kb_store_unavailable": "资料存储当前不可用",
                "kb_index_unavailable": "资料索引当前不可用",
                "skill_store_unavailable": "技能存储当前不可用",
            }.get(code, "所需存储当前不可用")
            recovery = "none"
        return Failure(code, message, source, stage, impact, recovery)
    if isinstance(error, TimeoutError):
        return Failure("timeout", "请求超时", source, stage, impact, "wait")
    if isinstance(error, ConnectionError):
        return Failure("connection_failed", "无法连接所需服务", source, stage, impact, "wait")
    if isinstance(error, sqlite3.Error):
        return Failure("storage_failed", "运行状态存储失败", "storage", stage, impact)
    if isinstance(error, OSError):
        return Failure("io_failed", "文件或连接读写失败", source, stage, impact)
    return Failure("unexpected", "处理时发生内部错误", source, stage, impact)


def log_failure(logger: logging.Logger, failure: Failure, error: Exception | None = None) -> None:
    """记录关联编号与调用位置；不输出异常值、局部变量或供应商响应正文。"""
    frames = (
        [
            (frame.filename, frame.lineno, frame.name)
            for frame in traceback.extract_tb(error.__traceback__)
        ]
        if error is not None
        else []
    )
    logger.error(
        "failure diagnostic_id=%s code=%s source=%s stage=%s exception=%s frames=%s",
        failure.diagnostic_id,
        failure.code,
        failure.source,
        failure.stage,
        type(error).__name__ if error is not None else None,
        frames,
    )
