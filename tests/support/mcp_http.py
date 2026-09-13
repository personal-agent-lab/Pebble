"""按真实 MCP 协议访问工具端点：进程内 ASGI 传输，不监听端口。

工具端的验证必须走协议本身：`httpx.ASGITransport` 把请求直接交给应用，
与生产环境唯一的差别是没有经过网络与 uvicorn。
"""

import json
from contextlib import asynccontextmanager

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


@asynccontextmanager
async def mcp_session(app, url: str):
    """连接 `app` 上 `url` 指向的工具端点，完成初始化后交出会话。"""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def tool_payload(result) -> dict:
    """工具结果里的结构化负载；成功与业务失败都用同一段 JSON 文本。"""
    return json.loads(result.content[0].text)
