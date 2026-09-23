"""进程内 MCP server：把一轮可见的工具经 streamable HTTP 暴露给 CLI 子进程。

CLI 只连接真实传输（stdio/sse/http/...），SDK 的进程内 `sdk` 类型不会建立连接，因此工具
不由 `create_sdk_mcp_server` 装配：应用进程自己提供端点，每轮输入登记一个一次性路径，
绑定该轮允许的工具、任务标识与草稿事件队列，轮次结束即撤销。

端点单独监听回环端口，不挂在业务应用上：对外转发只覆盖业务端口，工具端点结构上不可达。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from uuid import uuid4

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from server.errors import error_details
from server.tools.registry import ToolDefinition, ToolFileResult

logger = logging.getLogger(__name__)

MCP_MOUNT_PATH = "/mcp"
LOOPBACK_HOST = "127.0.0.1"
TOOL_SERVER_NAME = "pebble"
TOOL_ERROR_MESSAGE = "工具执行失败"
UNKNOWN_TOOL_MESSAGE = "本轮没有这个工具"
TARGET_TOOL_MESSAGE = "本轮只能读取或修改指定的待确认内容"

# 定向轮只读取与修改指定的待确认内容，任何会新建操作的工具都不在其中。
NEW_OPERATION_TOOLS = frozenset(
    {
        "gmail_prepare_reply",
        "gmail_prepare_email",
        "calendar_create_event",
    }
)


class ToolServer:
    """应用进程内的 MCP 端点：按轮次登记工具，模型只看到当轮允许的集合。"""

    def __init__(self) -> None:
        self._turns: dict[str, StreamableHTTPSessionManager] = {}

    @asynccontextmanager
    async def serve(
        self,
        tools: list[ToolDefinition],
        *,
        task_id: str,
        queued: asyncio.Queue,
        target_operation_id: str | None = None,
    ) -> AsyncIterator[str]:
        """登记一轮的工具，yield 该轮的 URL 路径；退出时撤销。"""
        token = uuid4().hex
        server = build_server(
            tools,
            task_id=task_id,
            target_operation_id=target_operation_id,
            queued=queued,
        )
        manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)
        async with manager.run():
            self._turns[token] = manager
            try:
                yield f"{MCP_MOUNT_PATH}/{token}"
            finally:
                self._turns.pop(token, None)

    @asynccontextmanager
    async def listen(self, host: str, port: int) -> AsyncIterator[None]:
        """在 `host:port` 上提供工具端点，退出时停止监听。"""
        app = Starlette(routes=[Mount(MCP_MOUNT_PATH, app=self)])
        # log_config=None：不重设全局日志，沿用外层服务的配置。
        server = _EmbeddedServer(
            uvicorn.Config(app, host=host, port=port, lifespan="off", log_config=None)
        )
        serving = asyncio.create_task(server.serve())
        while not server.started:
            if serving.done():
                # 端口被占用等启动失败时 uvicorn 已记录原因并退出，这里把结果原样抛出。
                await serving
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            server.should_exit = True
            await serving

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI 入口：路径最后一段是本轮令牌，未知令牌按不存在处理。"""
        if scope["type"] != "http":
            return
        token = scope["path"].strip("/").split("/")[-1]
        manager = self._turns.get(token)
        if manager is None:
            await Response("未知工具会话", status_code=404)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


class _EmbeddedServer(uvicorn.Server):
    """随业务应用生命周期运行的内嵌服务：信号交给外层服务处理，不另行接管。"""

    def capture_signals(self):
        return nullcontext()


def build_server(
    tools: list[ToolDefinition],
    *,
    task_id: str,
    queued: asyncio.Queue,
    target_operation_id: str | None = None,
) -> Server:
    """把本轮允许的工具装到一个 MCP server 上：清单与调用都只认这一份。"""
    server = Server(TOOL_SERVER_NAME, version="1.0.0")
    known = {tool.name: tool for tool in tools}

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(name=tool.name, description=tool.description, inputSchema=tool.parameters_schema)
            for tool in tools
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        definition = known.get(name)
        if definition is None:
            return error_result({"error": "unknown_tool", "message": UNKNOWN_TOOL_MESSAGE})
        return await invoke(
            definition,
            arguments,
            task_id=task_id,
            target_operation_id=target_operation_id,
            queued=queued,
        )

    return server


async def invoke(
    definition: ToolDefinition,
    arguments: dict,
    *,
    task_id: str,
    queued: asyncio.Queue,
    target_operation_id: str | None = None,
) -> CallToolResult:
    """执行一次工具调用：业务失败按统一错误词汇交回模型，成功事件按工具声明入队。"""
    fields = dict(arguments)
    if target_operation_id is not None and (
        definition.name in NEW_OPERATION_TOOLS
        or (
            definition.name
            in {
                "gmail_read_draft",
                "gmail_update_draft",
            }
            and fields.get("operation_id") != target_operation_id
        )
    ):
        return error_result({"error": "wrong_target", "message": TARGET_TOOL_MESSAGE})
    if definition.needs_task_id:
        fields["task_id"] = task_id
    try:
        result = await asyncio.to_thread(definition.func, **fields)
    except Exception as error:
        details = error_details(error)
        if details is None:
            logger.exception("工具 %s 执行失败", definition.name)
            details = {"error": "unexpected", "message": TOOL_ERROR_MESSAGE}
        return error_result(details)
    if definition.emits_draft_saved:
        queued.put_nowait(
            {
                "type": "draft_saved",
                "operation_id": result["operation_id"],
                "version": result["version"],
            }
        )
    if definition.notice_renderer is not None:
        queued.put_nowait({"type": "notice", "text": definition.notice_renderer(result)})
    if isinstance(result, ToolFileResult):
        metadata = {**result.metadata, "filename": result.filename, "mime_type": result.mime_type}
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=result.uri,
                        mimeType=result.mime_type,
                        blob=base64.b64encode(result.data).decode("ascii"),
                    ),
                ),
            ]
        )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    )


def error_result(details: dict) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(details, ensure_ascii=False))],
        isError=True,
    )
