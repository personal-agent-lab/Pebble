"""服务装配：创建 FastAPI 应用、初始化数据库、恢复中断工作并注册路由。

Agent 网关（Qoder Agent SDK）、Gmail 与新邮件检测的真实实现由 B 接入；本模块只做装配，
未接入时对应调用直接报错，不伪造行为。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.api.errors import install_error_handlers
from server.api.routes import router
from server.approval.service import ConfirmationService, recover_interrupted_executions
from server.db import init_db
from server.gateway.mail_source import MailSource
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore


def create_app(
    *,
    gateway=None,
    validate_reply_draft=None,
    send_reply=None,
    mail_source: MailSource | None = None,
) -> FastAPI:
    tasks = SessionStore()
    drafts = ReplyDraftStore(validate_reply_draft)
    confirmations = ConfirmationService(send_reply)
    agent = GatewayRuntime(gateway, confirmations=confirmations)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db()
        recover_interrupted_executions()
        agent.resume()
        # 邮件检测在恢复之后开始：重启遗留的调用先各自归位，再接受新的邮件输入。
        if mail_source is not None:
            await mail_source.start(agent)
        try:
            yield
        finally:
            if mail_source is not None:
                await mail_source.stop()
            await agent.close()

    app = FastAPI(title="Pebble", lifespan=lifespan)
    app.state.tasks = tasks
    app.state.drafts = drafts
    app.state.confirmations = confirmations
    app.state.agent = agent
    app.state.mail_source = mail_source
    install_error_handlers(app)
    app.include_router(router, prefix="/api")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from server.config import get_settings

    settings = get_settings()
    uvicorn.run("server.main:app", host=settings.host, port=settings.port, reload=True)
