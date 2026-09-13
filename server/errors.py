"""跨模块共享的业务异常；HTTP 映射留在 api。"""


class DependencyUnavailableError(Exception):
    """所需外部能力尚未接入，工作未被接受。"""


class NotFoundError(Exception):
    pass


class VersionConflictError(Exception):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"当前版本为 {current_version}")


class NotEditableError(Exception):
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"状态 {status} 不可编辑")


class SessionConflictError(Exception):
    pass
