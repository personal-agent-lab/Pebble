"""业务异常到 HTTP 错误响应的统一映射。

对象不存在为 404，版本、状态或会话冲突为 409，输入、草稿、记忆或资料校验失败与记忆容量不足
为 422，依赖、记忆或资料库不可用为 503；
响应体由 `server.errors.error_details` 给出，与交回模型的错误描述共用同一套名称和字段。
数据库异常不在此处理，按服务端错误返回。
"""

import logging

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
    SkillConflictError,
    SkillStoreUnavailableError,
    SkillUnknownError,
    SkillValidationError,
    TaskActiveError,
    TaskIdConflictError,
    TaskNotRunningError,
    VersionConflictError,
    error_details,
)
from server.failures import FailureError, exception_failure, log_failure

STATUS_CODES: tuple[tuple[type[Exception], int], ...] = (
    (DependencyUnavailableError, 503),
    (NotFoundError, 404),
    (VersionConflictError, 409),
    (NotEditableError, 409),
    (SessionConflictError, 409),
    (TaskActiveError, 409),
    (TaskIdConflictError, 409),
    (TaskNotRunningError, 409),
    (RetryUnavailableError, 409),
    (ModelValidationError, 422),
    (AttachmentValidationError, 422),
    (DraftValidationError, 422),
    (SkillValidationError, 422),
    (SkillConflictError, 409),
    (SkillUnknownError, 404),
    (SkillStoreUnavailableError, 503),
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
                failure = exception_failure(error, source="api", stage="request")
                body = {
                    **error_details(error),
                    "message": failure.message,
                    "failure": failure.payload(),
                }
                return JSONResponse(status_code=status_code, content=body)

            return respond

        app.add_exception_handler(error_type, handler())

    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        failure = exception_failure(error, source="api", stage="request")
        log_failure(logging.getLogger(__name__), failure, error)
        status = 500
        if isinstance(error, FailureError):
            status = {
                "model_content_filtered": 422,
                "model_refused": 422,
                "model_invalid_request": 422,
                "timeout": 504,
                "model_authentication": 503,
                "model_billing": 503,
                "model_rate_limited": 503,
                "model_unavailable": 503,
            }.get(failure.code, 502 if failure.source in {"model", "sdk"} else 500)
        return JSONResponse(
            status_code=status,
            content={
                "error": failure.code,
                "message": failure.message,
                "failure": failure.payload(),
            },
        )

    app.add_exception_handler(Exception, unexpected)
