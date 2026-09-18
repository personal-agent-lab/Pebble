"""服务装配：创建 FastAPI 应用、初始化数据库、恢复中断工作并注册路由。

依赖按参数注入，进程内不使用工具或存储的全局单例：Agent 网关、邮件校验、发送函数与新邮件
来源未接入时，相关调用直接报错，不伪造行为。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from server.agent.mcp import LOOPBACK_HOST, ToolServer
from server.agent.models import ModelCatalog
from server.api.access import AccessGuard, AccessPolicy
from server.api.errors import install_error_handlers
from server.api.routes import router
from server.api.web import WEB_DIST, SpaFiles
from server.approval.service import ConfirmationService
from server.attachments import AttachmentStore
from server.config import get_settings
from server.db import init_db
from server.errors import DependencyUnavailableError
from server.gateway.runtime import GatewayRuntime, MailSource
from server.memory.review import MemoryReviewScheduler
from server.memory.service import MemoryStore
from server.sessions.history import HistoryStore
from server.sessions.service import SessionStore
from server.tools.calendar.service import CalendarEventStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore

logger = logging.getLogger(__name__)


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
    reviews: MemoryReviewScheduler | None = None,
    memory_store: MemoryStore | None = None,
    kb_store: KbStore | None = None,
    history: HistoryStore | None = None,
    create_event=None,
    verify_event=None,
    model_catalog: ModelCatalog | None = None,
    attachments: AttachmentStore | None = None,
    tool_port: int | None = None,
    access: AccessPolicy | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    """装配应用。

    `tasks` / `drafts` / `confirmations` 供调用方先行构造：Agent 工具与 HTTP 必须共用同一组
    实例，而工具要在构造 gateway 之前绑定依赖。未传入时就地构造。

    `tool_server` 在 `tool_port` 上单独监听回环地址；`access` 为空时不做访问控制，
    只用于测试与本机开发；`web_dist` 存在构建产物时同源提供前端页面。
    """
    attachments = attachments if attachments is not None else AttachmentStore()
    model_catalog = model_catalog if model_catalog is not None else ModelCatalog()
    tasks = tasks if tasks is not None else SessionStore(attachments=attachments)
    drafts = drafts if drafts is not None else MailDraftStore()
    confirmations = (
        confirmations
        if confirmations is not None
        else ConfirmationService(
            send_message, verify_message, create_event=create_event, verify_event=verify_event
        )
    )
    reviews = reviews if reviews is not None else MemoryReviewScheduler()
    agent = GatewayRuntime(
        gateway,
        confirmations=confirmations,
        reviews=reviews,
        memory_store=memory_store,
        attachments=attachments,
        model_catalog=model_catalog,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if tool_server is not None:
                # 工具端点先于恢复与邮件检测就绪：恢复的调用一开始就要连它。
                await stack.enter_async_context(
                    tool_server.listen(LOOPBACK_HOST, tool_port or get_settings().tool_port)
                )
            async with run(app):
                yield

    @asynccontextmanager
    async def run(app: FastAPI) -> AsyncIterator[None]:
        init_db()
        confirmations.recover_interrupted_executions()
        agent.resume()
        # 启动即在后台预读模型目录：新任务页打开时通常已有目录，读取失败也不影响启动。
        model_catalog.refresh_in_background()
        # 邮件检测在恢复之后开始：重启遗留的调用先各自归位，再接受新的邮件输入。
        if mail_source is not None:
            await mail_source.start(agent)
        try:
            yield
        finally:
            if mail_source is not None:
                await mail_source.stop()
            await agent.close()
            await model_catalog.close()

    app = FastAPI(title="Pebble", lifespan=lifespan)
    if access is not None:
        app.add_middleware(AccessGuard, policy=access)
    app.state.tasks = tasks
    app.state.drafts = drafts
    app.state.confirmations = confirmations
    app.state.agent = agent
    app.state.mail_source = mail_source
    app.state.kb_store = kb_store
    app.state.memory_store = agent.memory_store
    app.state.history = history if history is not None else HistoryStore()
    app.state.model_catalog = model_catalog
    app.state.attachments = attachments
    # 可选外部服务的接入状态，由生产装配填写；测试装配不声明时健康检查不列出。
    app.state.services = {}
    install_error_handlers(app)
    app.include_router(router, prefix="/api")
    if web_dist is not None and (web_dist / "index.html").is_file():
        app.mount("/", SpaFiles(directory=web_dist, html=True), name="web")
    return app


def _optional_gmail(settings):
    """按凭证装配三个用途各自的 Gmail 客户端；未配置凭证时返回 None 与原因，不阻止启动。"""
    from server.tools.gmail.client import create_gmail_client

    try:
        return (
            create_gmail_client(settings),
            create_gmail_client(settings),
            create_gmail_client(settings),
        ), None
    except RuntimeError as error:
        return None, str(error)


def _optional_calendar(settings):
    """装配 iCloud 日历客户端；账号、密码文件或日历地址不全时返回 None 与原因。"""
    from server.tools.calendar.client import CalDAVCalendarClient

    try:
        return CalDAVCalendarClient(settings), None
    except DependencyUnavailableError as error:
        return None, str(error)


def create_production_app() -> FastAPI:
    """生产装配。邮件与日历是可选服务：缺凭证时关掉该服务的工具、检测与执行，其余照常启动。"""
    from functools import partial

    from server.agent.client import QoderGateway
    from server.agent.toolset import ToolDeps
    from server.tools.gmail.sender import send_message, verify_message
    from server.tools.gmail.sync import GmailSource

    settings = get_settings()
    attachments = AttachmentStore(settings)
    tasks = SessionStore(attachments=attachments)
    drafts = MailDraftStore()
    calendar_events = CalendarEventStore()
    memory_store = MemoryStore(settings.data_dir)
    kb_store = KbStore(settings.data_dir)
    history = HistoryStore()
    # 三处用途各自构造客户端：检测在自己的顺序轮询里，工具随模型并发调用，发送与核实同为
    # Confirmation 串行调用故共用一个；不共享其余 HTTP 连接。
    gmail, gmail_reason = _optional_gmail(settings)
    calendar_client, calendar_reason = _optional_calendar(settings)
    for name, reason in (("Gmail", gmail_reason), ("iCloud 日历", calendar_reason)):
        if reason is not None:
            logger.warning("%s 未接入，相关功能关闭：%s", name, reason)
    tool_client, confirmation_client, source_client = gmail or (None, None, None)
    tool_server = ToolServer()
    # 确认服务先于工具构造：日程直连创建工具在装配期就要绑定它。
    confirmations = ConfirmationService(
        partial(send_message, client=confirmation_client) if gmail else None,
        partial(verify_message, client=confirmation_client) if gmail else None,
        create_event=calendar_client.create_event if calendar_client else None,
        verify_event=calendar_client.verify_event if calendar_client else None,
    )
    app = create_app(
        gateway=QoderGateway(
            ToolDeps(
                # 邮件未接入时连草稿工具一起不给模型：起草出来的邮件永远发不出去。
                # HTTP 仍用同一个草稿存储，已有草稿照常可以查看。
                drafts=drafts if gmail else None,
                tasks=tasks,
                gmail=tool_client,
                memory_store=memory_store,
                kb_store=kb_store,
                history=history,
                calendar=calendar_client,
                calendar_events=calendar_events if calendar_client else None,
                confirmations=confirmations,
            ),
            tool_server,
            settings=settings,
        ),
        mail_source=GmailSource(source_client) if gmail else None,
        tasks=tasks,
        drafts=drafts,
        tool_server=tool_server,
        confirmations=confirmations,
        reviews=MemoryReviewScheduler(memory_store=memory_store),
        memory_store=memory_store,
        kb_store=kb_store,
        history=history,
        model_catalog=ModelCatalog(settings),
        attachments=attachments,
        tool_port=settings.tool_port,
        access=_access_policy(settings),
        web_dist=WEB_DIST,
    )
    app.state.services = {
        "gmail": _service_state(gmail_reason),
        "calendar": _service_state(calendar_reason),
    }
    return app


def _access_policy(settings) -> AccessPolicy | None:
    """访问控制缺配置时拒绝启动，不静默退回无校验；关闭只能显式声明。"""
    if settings.auth == "off":
        logger.warning("访问控制已关闭（PEBBLE_AUTH=off），只应在本机开发时使用")
        return None
    try:
        return AccessPolicy(settings.allowed_logins, settings.public_origin or "")
    except ValueError as error:
        raise RuntimeError(f"{error}；本机开发可设置 PEBBLE_AUTH=off") from error


def _service_state(reason: str | None) -> dict:
    if reason is None:
        return {"status": "ok", "detail": None}
    return {"status": "unconfigured", "detail": reason}


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "server.main:create_production_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=True,
    )
