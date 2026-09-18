"""访问控制：身份来自 Tailscale Serve 注入的请求头，写请求另校验来源。

服务只监听回环地址，外部请求都经 Tailscale Serve 转发；Serve 会先清掉客户端自带的同名头，
再按发起设备的登录账号写入 `Tailscale-User-Login`，因此这个头可以作为身份。本机进程能绕过
Serve 直连并伪造它，但本机进程本来就能读实例数据目录，不在防护范围内。

身份按设备认定，同一设备上的任何网页都会带着它向本服务发请求，所以会修改数据的请求还要求
`Origin` 等于对外地址，拦住其他网站诱导的写入。SSE 在建立连接时校验一次；撤销访问即在
Tailscale 移除设备或账号，连接随网络一起断开。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

IDENTITY_HEADER = b"tailscale-user-login"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FORBIDDEN_MESSAGE = "当前设备或账号无权访问 Pebble"
ORIGIN_MESSAGE = "请求来源不是 Pebble 页面，已拒绝"


@dataclass(frozen=True)
class AccessPolicy:
    allowed_logins: frozenset[str]
    public_origin: str

    def __post_init__(self) -> None:
        if not self.allowed_logins:
            raise ValueError("未配置 PEBBLE_ALLOWED_USERS：没有账号能访问")
        if not self.public_origin:
            raise ValueError("未配置 PEBBLE_PUBLIC_ORIGIN：无法校验写请求来源")
        object.__setattr__(self, "public_origin", self.public_origin.rstrip("/"))


class AccessGuard:
    """纯 ASGI 中间件：不包装响应体，SSE 等流式响应原样透传。"""

    def __init__(self, app: ASGIApp, policy: AccessPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        login = headers.get(IDENTITY_HEADER, b"").decode("latin-1").strip().lower()
        if login not in self.policy.allowed_logins:
            await deny(scope, send, "forbidden", FORBIDDEN_MESSAGE)
            return
        if scope["method"] not in SAFE_METHODS:
            origin = headers.get(b"origin", b"").decode("latin-1").rstrip("/")
            if origin != self.policy.public_origin:
                await deny(scope, send, "bad_origin", ORIGIN_MESSAGE)
                return
        await self.app(scope, receive, send)


async def deny(scope: Scope, send: Send, code: str, message: str) -> None:
    """接口返回统一错误结构，页面请求返回一句纯文本说明。"""
    if scope["path"].startswith("/api/"):
        body = json.dumps({"error": code, "message": message}, ensure_ascii=False).encode()
        content_type = b"application/json"
    else:
        body = message.encode()
        content_type = b"text/plain; charset=utf-8"
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
