"""进程内 MCP server：把一轮可见的工具经 streamable HTTP 暴露给 CLI 子进程。

CLI 只连接真实传输（stdio/sse/http/...），SDK 的进程内 `sdk` 类型不会建立连接，因此工具
不由 `create_sdk_mcp_server` 装配：应用进程自己提供端点，每轮输入登记一个一次性路径，
绑定该轮允许的工具、任务标识与草稿事件队列，轮次结束即撤销。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from server.errors import error_details
from server.tools.registry import ToolDefinition

logger = logging.getLogger(__name__)

MCP_MOUNT_PATH = "/mcp"
TOOL_SERVER_NAME = "pebble"
TOOL_ERROR_MESSAGE = "工具执行失败"
UNKNOWN_TOOL_MESSAGE = "本轮没有这个工具"


class ToolServer:
    """应用进程内的 MCP 端点：按轮次登记工具，模型只看到当轮允许的集合。"""

    def __init__(self) -> None:
        self._turns: dict[str, StreamableHTTPSessionManager] = {}

    @asynccontextmanager
    async def serve(
        self, tools: list[ToolDefinition], *, task_id: str, queued: asyncio.Queue
    ) -> AsyncIterator[str]:
        """登记一轮的工具，yield 该轮的 URL 路径；退出时撤销。"""
        token = uuid4().hex
        server = build_server(tools, task_id=task_id, queued=queued)
        manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)
        async with manager.run():
            self._turns[token] = manager
            try:
                yield f"{MCP_MOUNT_PATH}/{token}"
            finally:
                self._turns.pop(token, None)

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


def build_server(tools: list[ToolDefinition], *, task_id: str, queued: asyncio.Queue) -> Server:
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
        return await invoke(definition, arguments, task_id=task_id, queued=queued)

    return server


async def invoke(
    definition: ToolDefinition,
    arguments: dict,
    *,
    task_id: str,
    queued: asyncio.Queue,
) -> CallToolResult:
    """执行一次工具调用：业务失败按统一错误词汇交回模型，成功时把草稿事件入队。"""
    fields = dict(arguments)
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
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    )


def error_result(details: dict) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(details, ensure_ascii=False))],
        isError=True,
    )
