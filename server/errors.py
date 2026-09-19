"""跨模块共享的业务异常及其统一描述。

`error_details` 给出契约第 8 节各错误的名称与附带字段：`api/errors.py` 用它作为 HTTP 响应体，
Agent 工具边界用它把失败原因交回模型。两处共用同一套名称和字段，不各写一份。
"""


class DependencyUnavailableError(Exception):
    """所需外部能力尚未接入，工作未被接受。"""


class NotFoundError(Exception):
    pass


class VersionConflictError(Exception):
    def __init__(self, current_version):
        self.current_version = current_version
        super().__init__(f"当前版本为 {current_version}")


class NotEditableError(Exception):
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"状态 {status} 不可编辑")


class SessionConflictError(Exception):
    pass


class TaskActiveError(Exception):
    """任务仍有待执行或执行中的运行，删除会被拒绝。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"任务仍在运行：{task_id}")


class DraftValidationError(Exception):
    """待确认内容校验未通过；`errors` 为字段与原因列表。"""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(str(errors))


class SkillValidationError(Exception):
    """Skill 字段校验未通过；`errors` 为字段与原因列表。"""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(str(errors))


def error_details(error: Exception) -> dict | None:
    """已知业务异常的名称与附带字段；其他异常返回 None，由调用方按未预期错误处理。"""
    if isinstance(error, DependencyUnavailableError):
        return {"error": "unavailable", "message": str(error)}
    if isinstance(error, NotFoundError):
        return {"error": "not_found", "message": f"对象不存在：{error}"}
    if isinstance(error, VersionConflictError):
        return {
            "error": "version_conflict",
            "message": str(error),
            "current_version": error.current_version,
        }
    if isinstance(error, NotEditableError):
        return {"error": "not_editable", "message": str(error), "status": error.status}
    if isinstance(error, SessionConflictError):
        return {"error": "session_conflict", "message": "任务已关联不同会话"}
    if isinstance(error, TaskActiveError):
        return {"error": "task_active", "message": "任务正在运行，结束后才能删除"}
    if isinstance(error, DraftValidationError):
        return {"error": "invalid_draft", "message": "待确认内容未通过校验", "errors": error.errors}
    if isinstance(error, SkillValidationError):
        return {"error": "invalid_skill", "message": "Skill 字段校验未通过", "errors": error.errors}
    return None
