"""服务装配：创建 FastAPI 应用、初始化数据库、恢复中断工作并注册路由。

依赖按参数注入，进程内不使用工具或存储的全局单例：Agent 网关、邮件校验、发送函数与新邮件
来源未接入时，相关调用直接报错，不伪造行为。
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
    tasks: SessionStore | None = None,
    drafts: ReplyDraftStore | None = None,
) -> FastAPI:
    """装配应用。

    `tasks` / `drafts` 供调用方先行构造：Agent 工具与 HTTP 必须共用同一组存储实例，
    而工具要在构造 gateway 之前绑定依赖。未传入时按 `validate_reply_draft` 就地构造。
    """
    tasks = tasks if tasks is not None else SessionStore()
    drafts = drafts if drafts is not None else ReplyDraftStore(validate_reply_draft)
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


def create_production_app() -> FastAPI:
    import os
    from functools import partial

    from server.agent.sdk_client import QoderGateway
    from server.agent.toolset import build_tools
    from server.background import GmailSource
    from server.config import get_settings
    from server.tools.gmail.client import create_gmail_client
    from server.tools.gmail.sender import send_reply
    from server.tools.gmail.validator import validate_reply_draft

    settings = get_settings()
    os.environ["QODERCN_CONFIG_DIR"] = str(settings.data_dir / "agent" / "config-cn")
    tasks = SessionStore()
    drafts = ReplyDraftStore(validate_reply_draft)
    # 三处用途各自构造客户端：检测在自己的顺序轮询里，工具随模型并发调用，发送由
    # Confirmation 串行调用；不共享 HTTP 连接，凭证缺失在这里就失败，不进入运行期。
    tools = build_tools(drafts=drafts, tasks=tasks, gmail=create_gmail_client(settings))
    return create_app(
        gateway=QoderGateway(tools),
        validate_reply_draft=validate_reply_draft,
        send_reply=partial(send_reply, client=create_gmail_client(settings)),
        mail_source=GmailSource(create_gmail_client(settings)),
        tasks=tasks,
        drafts=drafts,
    )


if __name__ == "__main__":
    import uvicorn

    from server.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "server.main:create_production_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=True,
    )
