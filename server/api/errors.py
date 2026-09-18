"""业务异常到 HTTP 错误响应的统一映射。

对象不存在为 404，版本、状态或会话冲突为 409，输入、草稿、记忆或资料校验失败与记忆容量不足
为 422，依赖、记忆或资料库不可用为 503；
响应体由 `server.errors.error_details` 给出，与交回模型的错误描述共用同一套名称和字段。
数据库异常不在此处理，按服务端错误返回。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.errors import (
    AttachmentValidationError,
    DependencyUnavailableError,
    DraftValidationError,
    HistoryValidationError,
    KbIndexUnavailableError,
    KbStoreUnavailableError,
    KbValidationError,
    MemoryFullError,
    MemoryStoreUnavailableError,
    MemoryValidationError,
    ModelValidationError,
    NotEditableError,
    NotFoundError,
    RetryUnavailableError,
    SessionConflictError,
    TaskActiveError,
    TaskIdConflictError,
    VersionConflictError,
    error_details,
)

STATUS_CODES: tuple[tuple[type[Exception], int], ...] = (
    (DependencyUnavailableError, 503),
    (NotFoundError, 404),
    (VersionConflictError, 409),
    (NotEditableError, 409),
    (SessionConflictError, 409),
    (TaskActiveError, 409),
    (TaskIdConflictError, 409),
    (RetryUnavailableError, 409),
    (ModelValidationError, 422),
    (AttachmentValidationError, 422),
    (DraftValidationError, 422),
    (HistoryValidationError, 422),
    (KbValidationError, 422),
    (MemoryValidationError, 422),
    (MemoryFullError, 422),
    (MemoryStoreUnavailableError, 503),
    (KbStoreUnavailableError, 503),
    (KbIndexUnavailableError, 503),
)


def install_error_handlers(app: FastAPI) -> None:
    for error_type, status_code in STATUS_CODES:

        def handler(status_code=status_code):
            async def respond(request: Request, error: Exception) -> JSONResponse:
                return JSONResponse(status_code=status_code, content=error_details(error))

            return respond

        app.add_exception_handler(error_type, handler())
