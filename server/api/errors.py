"""业务异常到 HTTP 错误响应的统一映射。

对象不存在为 404，版本、状态或会话冲突为 409，输入或草稿校验失败为 422；
错误响应保留当前版本、状态或字段原因。数据库异常不在此处理，按服务端错误返回。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.errors import (
    DependencyUnavailableError,
    NotEditableError,
    NotFoundError,
    SessionConflictError,
    VersionConflictError,
)
from server.tools.gmail.service import DraftValidationError


def error_body(name: str, message: str, **fields: object) -> dict:
    return {"error": name, "message": message, **fields}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DependencyUnavailableError)
    async def unavailable(request: Request, error: DependencyUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content=error_body("unavailable", str(error)))

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404, content=error_body("not_found", f"对象不存在：{error}")
        )

    @app.exception_handler(VersionConflictError)
    async def version_conflict(request: Request, error: VersionConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_body(
                "version_conflict", str(error), current_version=error.current_version
            ),
        )

    @app.exception_handler(NotEditableError)
    async def not_editable(request: Request, error: NotEditableError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_body("not_editable", str(error), status=error.status),
        )

    @app.exception_handler(SessionConflictError)
    async def session_conflict(request: Request, error: SessionConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_body("session_conflict", "任务已关联不同会话"),
        )

    @app.exception_handler(DraftValidationError)
    async def invalid_draft(request: Request, error: DraftValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body("invalid_draft", "邮件草稿未通过校验", errors=error.errors),
        )
