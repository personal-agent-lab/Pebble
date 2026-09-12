"""服务装配：创建 FastAPI 应用、初始化数据库、恢复中断工作并注册路由。

Agent 网关（Qoder Agent SDK）与 Gmail 的真实实现由 B 接入；本模块只做装配，
未接入时对应调用直接报错，不伪造行为。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.api.errors import install_error_handlers
from server.api.routes import router
from server.approval.service import ConfirmationService, recover_interrupted_executions
from server.db import init_db
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore


def create_app(*, gateway=None, validate_reply_draft=None, send_reply=None) -> FastAPI:
    tasks = SessionStore()
    drafts = ReplyDraftStore(validate_reply_draft)
    confirmations = ConfirmationService(send_reply)
    agent = GatewayRuntime(gateway, confirmations=confirmations)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db()
        recover_interrupted_executions()
        agent.resume()
        try:
            yield
        finally:
            await agent.close()

    app = FastAPI(title="Pebble", lifespan=lifespan)
    app.state.tasks = tasks
    app.state.drafts = drafts
    app.state.confirmations = confirmations
    app.state.agent = agent
    install_error_handlers(app)
    app.include_router(router, prefix="/api")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from server.config import get_settings

    settings = get_settings()
    uvicorn.run("server.main:app", host=settings.host, port=settings.port, reload=True)
