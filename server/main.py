"""服务装配：创建 FastAPI 应用、初始化数据库、恢复中断工作并注册路由。

依赖按参数注入，进程内不使用工具或存储的全局单例：Agent 网关、邮件校验、发送函数与新邮件
来源未接入时，相关调用直接报错，不伪造行为。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.api.errors import install_error_handlers
from server.api.routes import router
from server.approval.service import ConfirmationService
from server.db import init_db
from server.gateway.runtime import GatewayRuntime, MailSource
from server.sessions.service import SessionStore
from server.tools.calendar.service import CalendarEventStore
from server.tools.gmail.service import MailDraftStore


def create_app(
    *,
    gateway=None,
    send_message=None,
    verify_message=None,
    mail_source: MailSource | None = None,
    tasks: SessionStore | None = None,
    drafts: MailDraftStore | None = None,
    tool_server: ToolServer | None = None,
    confirmations: ConfirmationService | None = None,
    create_event=None,
    verify_event=None,
) -> FastAPI:
    """装配应用。

    `tasks` / `drafts` / `confirmations` 供调用方先行构造：Agent 工具与 HTTP 必须共用同一组
    实例，而工具要在构造 gateway 之前绑定依赖。未传入时就地构造。
    """
    tasks = tasks if tasks is not None else SessionStore()
    drafts = drafts if drafts is not None else MailDraftStore()
    confirmations = (
        confirmations
        if confirmations is not None
        else ConfirmationService(
            send_message, verify_message, create_event=create_event, verify_event=verify_event
        )
    )
    agent = GatewayRuntime(gateway, confirmations=confirmations)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db()
        confirmations.recover_interrupted_executions()
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
    if tool_server is not None:
        # 工具端点挂在 /api 之外：CLI 子进程按回环地址直连，不经过业务路由与错误处理。
        app.mount(MCP_MOUNT_PATH, tool_server)
    app.state.tasks = tasks
    app.state.drafts = drafts
    app.state.confirmations = confirmations
    app.state.agent = agent
    app.state.mail_source = mail_source
    install_error_handlers(app)
    app.include_router(router, prefix="/api")
    return app


def create_production_app() -> FastAPI:
    from functools import partial

    from server.agent.client import QoderGateway
    from server.agent.toolset import ToolDeps
    from server.config import get_settings
    from server.tools.calendar.client import CalDAVCalendarClient
    from server.tools.gmail.client import create_gmail_client
    from server.tools.gmail.sender import send_message, verify_message
    from server.tools.gmail.sync import GmailSource

    settings = get_settings()
    tasks = SessionStore()
    drafts = MailDraftStore()
    calendar_events = CalendarEventStore()
    # 三处用途各自构造客户端：检测在自己的顺序轮询里，工具随模型并发调用，发送与核实同为
    # Confirmation 串行调用故共用一个；不共享其余 HTTP 连接，凭证缺失在这里就失败。
    tool_client = create_gmail_client(settings)
    confirmation_client = create_gmail_client(settings)
    calendar_client = CalDAVCalendarClient(settings)
    tool_server = ToolServer()
    # 确认服务先于工具构造：日程直连创建工具在装配期就要绑定它。
    confirmations = ConfirmationService(
        partial(send_message, client=confirmation_client),
        partial(verify_message, client=confirmation_client),
        create_event=calendar_client.create_event,
        verify_event=calendar_client.verify_event,
    )
    return create_app(
        gateway=QoderGateway(
            ToolDeps(
                drafts=drafts,
                tasks=tasks,
                gmail=tool_client,
                calendar=calendar_client,
                calendar_events=calendar_events,
                confirmations=confirmations,
            ),
            tool_server,
            settings=settings,
        ),
        mail_source=GmailSource(create_gmail_client(settings)),
        tasks=tasks,
        drafts=drafts,
        tool_server=tool_server,
        confirmations=confirmations,
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
