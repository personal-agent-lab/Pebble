"""跨模块共享的业务异常及其统一描述。

`error_details` 给出契约第 8 节各错误的名称与附带字段：`api/errors.py` 用它作为 HTTP 响应体，
Agent 工具边界用它把失败原因交回模型。两处共用同一套名称和字段，不各写一份。
"""


class DependencyUnavailableError(Exception):
    """所需外部能力尚未接入，工作未被接受。"""


class ModelValidationError(Exception):
    def __init__(self, model: str):
        self.model = model
        super().__init__(f"模型当前不可用：{model}")


class AttachmentValidationError(Exception):
    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(str(errors))


class NotFoundError(Exception):
    pass


class VersionConflictError(Exception):
    def __init__(self, current_version: int | str):
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


class TaskIdConflictError(Exception):
    """客户端给出的任务标识已被非同类任务占用，不能当作重复提交返回。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"任务标识已被占用：{task_id}")


class RetryUnavailableError(Exception):
    """最后一轮不是可重试的已中断用户消息。"""

    def __init__(self):
        super().__init__("只有最后一轮已中断的用户消息可以重试")


class DraftValidationError(Exception):
    """待确认内容校验未通过；`errors` 为字段与原因列表。"""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(str(errors))


class SkillValidationError(Exception):
    """Skill 字段校验失败。"""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(str(errors))


class MemoryValidationError(Exception):
    """长期记忆操作的字段或定位条件不合法；`memory` 是交回模型重新定位用的带锚点最新内容。"""

    def __init__(self, errors: list[dict[str, str]], memory: dict | None = None):
        self.errors = errors
        self.memory = memory
        super().__init__(str(errors))


MEMORY_LABELS = {"user": "关于你", "memory": "事实与约定"}


class MemoryFullError(Exception):
    """长期记忆文件超过该目标的字符容量。"""

    def __init__(self, target: str, used: int, limit: int, memory: dict | None = None):
        self.target = target
        self.memory = memory
        self.used = used
        self.limit = limit
        label = MEMORY_LABELS.get(target, target)
        super().__init__(f"“{label}”放不下：保存后需要 {used} 个字符，上限为 {limit}")


class MemoryStoreUnavailableError(Exception):
    """长期记忆文件当前不可读写。"""


class HistoryValidationError(Exception):
    """历史检索的输入不合法；`errors` 为字段与原因列表。"""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(str(errors))


class KbValidationError(Exception):
    """资料库操作的字段或路径不合法；`errors` 为字段与原因列表。

    按行修改的锚点失效时，`document` 附上该资料带锚点的最新正文与版本，交回模型重新定位。
    """

    def __init__(self, errors: list[dict[str, str]], document: dict | None = None):
        self.errors = errors
        self.document = document
        super().__init__(str(errors))


class KbStoreUnavailableError(Exception):
    """资料文件或本地版本仓库当前不可用。"""


class KbIndexUnavailableError(Exception):
    """资料索引缺失、损坏或无法重建；此时不返回可能过期的旧索引结果。"""


def error_details(error: Exception) -> dict | None:
    """已知业务异常的名称与附带字段；其他异常返回 None，由调用方按未预期错误处理。"""
    if isinstance(error, DependencyUnavailableError):
        return {"error": "unavailable", "message": str(error)}
    if isinstance(error, ModelValidationError):
        return {"error": "invalid_model", "message": str(error), "model": error.model}
    if isinstance(error, AttachmentValidationError):
        return {
            "error": "invalid_attachment",
            "message": "附件未通过校验",
            "errors": error.errors,
        }
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
    if isinstance(error, TaskIdConflictError):
        return {"error": "task_id_conflict", "message": str(error)}
    if isinstance(error, RetryUnavailableError):
        return {"error": "retry_unavailable", "message": str(error)}
    if isinstance(error, DraftValidationError):
        return {"error": "invalid_draft", "message": "待确认内容未通过校验", "errors": error.errors}
    if isinstance(error, SkillValidationError):
        return {"error": "invalid_skill", "message": "Skill 字段校验未通过", "errors": error.errors}
    if isinstance(error, MemoryValidationError):
        return {
            "error": "invalid_memory",
            "message": "长期记忆操作未通过校验",
            "errors": error.errors,
            **({"memory": error.memory} if error.memory is not None else {}),
        }
    if isinstance(error, MemoryFullError):
        return {
            "error": "memory_full",
            "message": str(error),
            "target": error.target,
            "used": error.used,
            "limit": error.limit,
            **({"memory": error.memory} if error.memory is not None else {}),
        }
    if isinstance(error, MemoryStoreUnavailableError):
        return {"error": "memory_store_unavailable", "message": str(error)}
    if isinstance(error, HistoryValidationError):
        return {"error": "invalid_history", "message": "历史检索未通过校验", "errors": error.errors}
    if isinstance(error, KbValidationError):
        return {
            "error": "invalid_kb",
            "message": "资料库操作未通过校验",
            "errors": error.errors,
            **({"document": error.document} if error.document is not None else {}),
        }
    if isinstance(error, KbStoreUnavailableError):
        return {"error": "kb_store_unavailable", "message": str(error)}
    if isinstance(error, KbIndexUnavailableError):
        return {"error": "kb_index_unavailable", "message": str(error)}
    return None
