"""业务异常到 HTTP 错误响应的统一映射。

对象不存在为 404，版本、状态或会话冲突为 409，输入或草稿校验失败为 422；
响应体由 `server.errors.error_details` 给出，与交回模型的错误描述共用同一套名称和字段。
数据库异常不在此处理，按服务端错误返回。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.errors import (
    DependencyUnavailableError,
    DraftValidationError,
    NotEditableError,
    NotFoundError,
    SessionConflictError,
    SkillValidationError,
    TaskActiveError,
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
    (DraftValidationError, 422),
    (SkillValidationError, 422),
)


def install_error_handlers(app: FastAPI) -> None:
    for error_type, status_code in STATUS_CODES:

        def handler(status_code=status_code):
            async def respond(request: Request, error: Exception) -> JSONResponse:
                return JSONResponse(status_code=status_code, content=error_details(error))

            return respond

        app.add_exception_handler(error_type, handler())
