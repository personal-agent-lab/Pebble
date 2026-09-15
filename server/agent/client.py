"""Qoder Agent SDK 网关：把一轮输入变成契约事件流，并把工具接回本进程的服务。

每次调用独立启动 CLI 子进程：新会话由 CLI 生成会话标识，后续轮次用 `resume` 接续；进程
退出后 SDK 自己保存会话状态，网页可见时间线由应用运行时单独持久化。

模型可见的工具只有本进程 MCP server（名字 `pebble`）里的只读与本地写工具：每轮在
`agent/mcp.py` 的端点上登记一个一次性路径，CLI 经回环地址连接，内置工具与本机设置一律
关闭。真实发送不在这里、也不经过模型：它由 Confirmation 调用。网关不区分触发来源：
一轮的消息与材料由调度层与触发域组装后经 `stream_turn` 传入。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from qodercn_agent_sdk import (
    AssistantMessage,
    HookMatcher,
    QoderAgentOptions,
    QoderSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    access_token,
)

from server.agent import context
from server.agent.mcp import TOOL_SERVER_NAME, ToolServer
from server.agent.prompt import TITLE_PROMPT
from server.agent.toolset import (
    ALLOWED_EFFECTS,
    ToolDeps,
    build_tools,
    exposed_tools,
)
from server.config import Settings, get_settings
from server.errors import DependencyUnavailableError
from server.gateway.agent_contract import AgentEvent, AgentProtocolError, Turn
from server.memory.service import MemoryStore
from server.tools.registry import ToolDefinition

CONFIG_DIR_ENV = "QODERCN_CONFIG_DIR"
LOOPBACK_HOST = "127.0.0.1"

# BYOK 供应商标识登记：SDK 目录由 CLI 运行时下发，这里只登记已向目录确认过的标识，
# 未登记的一律在装配期报错，接入新供应商时补充本表。目录中所有模型的协议风格都是
# openai，因此风格不再按供应商推导。
BYOK_PROVIDERS = frozenset(
    {
        "bailian",  # Alibaba Cloud Model Studio
        "deepseek",
        "kimi",
        "minimax",
        "qwencloud-cn",
        "xiaomi-china",  # Xiaomi MIMO
        "zhipu",  # Z.ai
    }
)
BYOK_STYLE = "openai"

NO_TERMINAL_MESSAGE = "SDK 调用未给出结束事件"
MODEL_ERROR_MESSAGE = "模型调用失败"
COMPACTION_ERROR_MESSAGE = "短期上下文压缩失败"


def turn_context_hooks(additional_context: str):
    """在本轮 CLI 进程启动或恢复时注入应用材料。"""
    if not additional_context:
        return None

    async def inject(_input, _tool_use_id, _hook_context):
        # SDK hook 返回的是协议字段，保持 camelCase；snake_case 不会被 CLI 识别。
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": additional_context,
            }
        }

    return {"SessionStart": [HookMatcher(hooks=[inject])]}


class QoderGateway:
    """`AgentGateway` 的 SDK 实现：依赖在构造时装配一次，每次调用各自启动子进程。"""

    def __init__(
        self, deps: ToolDeps, tool_server: ToolServer, *, settings: Settings | None = None
    ):
        self.settings = settings or get_settings()
        self.tool_server = tool_server
        self.memory_store = deps.memory_store or MemoryStore(self.settings.data_dir)
        agent_dir = self.settings.data_dir / "agent"
        self.workspace = agent_dir / "workspace"
        config_dir = agent_dir / "config"
        self.workspace.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        # 会话记录的读写都以这里为根：本进程读历史时查环境变量，子进程按继承的环境写入，
        # 两者必须指向同一目录，否则重启后读不到会话。
        os.environ[CONFIG_DIR_ENV] = str(config_dir)
        self.tools = build_tools(replace(deps, memory_store=self.memory_store))
        self._check_model_config()

    # ---------- 输入入口 ----------

    def stream_turn(self, turn: Turn) -> AsyncIterator[AgentEvent]:
        """执行一轮调用；消息与材料由调用方组装，网关不区分触发来源。"""
        return self._stream(turn)

    async def generate_title(self, text: str) -> str:
        """一次性标题生成：无工具、不接续会话，也不进入任务的对话历史。"""
        parts: list[str] = []
        async with QoderSDKClient(self._title_options()) as client:
            await client.query(text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    if message.is_error:
                        detail = (message.result or "").strip() or MODEL_ERROR_MESSAGE
                        raise AgentProtocolError(detail)
                    return "".join(parts).strip()
        raise AgentProtocolError(NO_TERMINAL_MESSAGE)

    # ---------- 调用执行 ----------

    async def _stream(self, turn: Turn) -> AsyncIterator[AgentEvent]:
        queued: asyncio.Queue[AgentEvent] = asyncio.Queue()
        visible = exposed_tools(self.tools, allowed=ALLOWED_EFFECTS[turn.kind])
        announced: str | None = None
        streamed = False
        async with self.tool_server.serve(
            visible,
            task_id=turn.task_id,
            target_operation_id=turn.target_operation_id,
            queued=queued,
        ) as path:
            options = self._options(turn, visible=visible, path=path)
            async with QoderSDKClient(options) as client:
                if turn.sdk_session_id is not None:
                    await self._compact_if_needed(client)
                await client.query(turn.message)
                async for message in client.receive_response():
                    # 工具事件在产生它的那次调用之后、模型的下一条消息之前送出。
                    while not queued.empty():
                        yield queued.get_nowait()
                    if isinstance(message, ResultMessage):
                        yield _result_event(message)
                        return
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        session_id = message.data.get("session_id")
                        # 同一会话一轮里会上报多次：只广播第一次，标识真的变化时照常上报。
                        if session_id and session_id != announced:
                            announced = session_id
                            yield {"type": "session", "sdk_session_id": session_id}
                    elif isinstance(message, StreamEvent):
                        text = _delta_text(message.event)
                        if text:
                            streamed = True
                            yield {"type": "text", "text": text}
                    elif isinstance(message, AssistantMessage):
                        # 去重只对本条消息生效：已转发它的增量输出就跳过整段文本，随后重置标志；
                        # 整轮共用会让缺少增量的后续消息被误判为重复而整段丢失。
                        if not streamed:
                            for block in message.content:
                                if isinstance(block, TextBlock) and block.text.strip():
                                    yield {"type": "text", "text": block.text}
                        streamed = False
        while not queued.empty():
            yield queued.get_nowait()
        yield {"type": "error", "message": NO_TERMINAL_MESSAGE}

    async def _compact_if_needed(self, client: QoderSDKClient) -> None:
        """运行时未启用自动压缩时，在达到其阈值后先完成手动压缩。"""
        usage = await client.get_context_usage()
        automatic = usage["autoCompact"]
        if automatic["enabled"]:
            return
        if usage["contextWindow"]["usedPercentage"] < automatic["thresholdPercentage"]:
            return

        compacted = False
        await client.query("/compact")
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
                compacted = True
            elif isinstance(message, ResultMessage) and message.is_error:
                detail = (message.result or "").strip() or COMPACTION_ERROR_MESSAGE
                raise AgentProtocolError(detail)
        if not compacted:
            raise AgentProtocolError(COMPACTION_ERROR_MESSAGE)

    def _options(
        self, turn: Turn, *, visible: list[ToolDefinition], path: str
    ) -> QoderAgentOptions:
        snapshot = self.memory_store.snapshot()
        memory_materials = tuple(
            context.Material(title, snapshot[target]["content"])
            for target, title in (("user", "关于你"), ("memory", "事实与约定"))
            if snapshot[target]["content"]
        )
        ctx = context.assemble(materials=(*memory_materials, *turn.materials))
        return QoderAgentOptions(
            # 内置工具与本机设置一律关闭：模型能看到的只有本轮 MCP 端点里的工具。
            tools=[],
            allowed_tools=[f"mcp__{TOOL_SERVER_NAME}__{definition.name}" for definition in visible],
            mcp_servers={
                TOOL_SERVER_NAME: {"type": "http", "url": self._tool_url(path)},
            },
            allowed_mcp_server_names=[TOOL_SERVER_NAME],
            strict_mcp_config=True,
            setting_sources=[],
            skills=ctx.skills,
            system_prompt=ctx.system_prompt,
            hooks=turn_context_hooks(ctx.additional_context),
            cwd=self.workspace,
            resume=turn.sdk_session_id,
            include_partial_messages=True,
            auth=self._auth(),
            **self._model_options(),
        )

    def _title_options(self) -> QoderAgentOptions:
        return QoderAgentOptions(
            tools=[],
            allowed_tools=[],
            mcp_servers={},
            allowed_mcp_server_names=[],
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            system_prompt=TITLE_PROMPT,
            cwd=self.workspace,
            include_partial_messages=False,
            auth=self._auth(),
            **self._model_options(hosted_model=self.settings.title_model),
        )

    def _tool_url(self, path: str) -> str:
        return f"http://{LOOPBACK_HOST}:{self.settings.port}{path}"

    def _auth(self) -> Any:
        token = self.settings.qoder_token
        if token is None:
            raise DependencyUnavailableError("未配置 Qoder 访问令牌")
        return access_token(token.get_secret_value())

    def _check_model_config(self) -> None:
        """BYOK 三项要么齐全且供应商已登记，要么都不配，缺项或写错都在装配期报错。

        只配一部分就静默退回托管模型，会让调用方以为在用自己的账号与额度，
        实际请求却去了别处。
        """
        settings = self.settings
        if not settings.model_provider and not settings.model_api_key:
            return
        missing = [
            name
            for name, value in (
                ("PEBBLE_MODEL_PROVIDER", settings.model_provider),
                ("PEBBLE_MODEL_API_KEY", settings.model_api_key),
                ("PEBBLE_QODER_MODEL", settings.qoder_model),
            )
            if not value
        ]
        if missing:
            raise DependencyUnavailableError(f"BYOK 配置不完整，缺少：{', '.join(missing)}")
        if settings.model_provider not in BYOK_PROVIDERS:
            raise DependencyUnavailableError(
                f"未登记的模型供应商：{settings.model_provider}，"
                f"可选：{', '.join(sorted(BYOK_PROVIDERS))}"
            )

    def _model_options(self, *, hosted_model: str | None = None) -> dict[str, Any]:
        """托管模型直接给型号名；配了第三方模型就转成 BYOK，凭证只在这里读取。

        `hosted_model` 只替换托管型号。BYOK 的密钥与供应商标识绑定，换型号就需要另一份
        凭证，所以配了 BYOK 时忽略它。
        """
        settings = self.settings
        if settings.model_provider:
            custom: dict[str, Any] = {
                "provider": settings.model_provider,
                "model": settings.qoder_model,
                "api_key": settings.model_api_key.get_secret_value(),
                "style": BYOK_STYLE,
            }
            if settings.model_base_url:
                custom["url"] = settings.model_base_url
            return {"resolve_model": lambda _context: {"model": custom}}
        return {"model": hosted_model or settings.qoder_model}


def _result_event(message: ResultMessage) -> AgentEvent:
    if message.is_error:
        detail = (message.result or "").strip() or MODEL_ERROR_MESSAGE
        return {"type": "error", "message": detail}
    return {"type": "done"}


def _delta_text(event: dict[str, Any]) -> str:
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return ""
    return delta.get("text", "")
