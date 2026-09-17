"""Qoder Agent SDK 网关：把一轮输入变成契约事件流，并把工具接回本进程的服务。

每次调用独立启动 CLI 子进程：新会话由 CLI 生成会话标识，后续轮次用 `resume` 接续；进程
退出后 SDK 自己保存会话状态，网页可见时间线由应用运行时单独持久化。

模型可见的工具是本进程 MCP server（名字 `pebble`）里的只读与本地写工具，外加用户亲自
发起轮次里的内置联网查询（`WEB_TOOLS`）：每轮在 `agent/mcp.py` 的端点上登记一个一次性
路径，CLI 经回环地址连接，本机设置一律关闭。真实发送不在这里、也不经过模型：它由
Confirmation 调用。网关不区分触发来源：一轮的消息与材料由调度层与触发域组装后经
`stream_turn` 传入。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from qodercn_agent_sdk import (
    AssistantMessage,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    QoderAgentOptions,
    QoderSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    access_token,
)

from server.agent import context
from server.agent.mcp import TOOL_ERROR_MESSAGE, TOOL_SERVER_NAME, ToolServer
from server.agent.prompt import TITLE_PROMPT
from server.agent.toolset import (
    ALLOWED_EFFECTS,
    ToolDeps,
    TurnKind,
    build_tools,
    exposed_tools,
)
from server.config import Settings, get_settings
from server.errors import DependencyUnavailableError, error_details
from server.gateway.agent_contract import AgentEvent, AgentProtocolError, Turn
from server.memory.service import MemoryStore
from server.tools.memory.tools import judge_registry, review_registry
from server.tools.personal_kb.catalog import CATALOG_TITLE
from server.tools.registry import ToolDefinition, activity

logger = logging.getLogger(__name__)

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

# SDK 内置的联网查询工具：搜索请求与页面抓取都经 Qoder 后端代理，只在用户亲自发起的
# 轮次暴露。触发轮与结果回传轮的输入来自外部内容，不能让其指令驱动网络请求把内容
# 带出实例；WebFetch 是对指定页面的只读抓取，与 WebSearch 合起来才是完整的查资料能力。
WEB_TOOLS = ("WebSearch", "WebFetch")


# 内置联网工具不经本进程注册表，步骤说明在开放它们的这一层给出。
WEB_TOOL_ACTIVITIES = {
    "WebSearch": ("正在联网搜索", "query"),
    "WebFetch": ("正在读取网页", "url"),
}


async def authorize_web_tool(tool_name: str, _input: dict, _context: Any):
    """批准本轮已显式开放的只读联网工具，拒绝所有意外权限请求。"""
    if tool_name in WEB_TOOLS:
        return PermissionResultAllow()
    return PermissionResultDeny(message=f"未授权的工具：{tool_name}")


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


def _recording(definition: ToolDefinition, records: list[dict]) -> ToolDefinition:
    """包一层记录：判断提示要按真实工具结果生成，不能用模型的自述。"""

    def recorded(**kwargs):
        try:
            result = definition.func(**kwargs)
        except Exception as error:
            details = error_details(error)
            if details is None:
                details = {"error": "unexpected", "message": TOOL_ERROR_MESSAGE}
            records.append({"tool": definition.name, "arguments": kwargs, "error": details})
            raise
        records.append({"tool": definition.name, "arguments": kwargs, "result": result})
        return result

    return replace(definition, func=recorded)


class QoderGateway:
    """`AgentGateway` 的 SDK 实现：依赖在构造时装配一次，每次调用各自启动子进程。"""

    def __init__(
        self, deps: ToolDeps, tool_server: ToolServer, *, settings: Settings | None = None
    ):
        self.settings = settings or get_settings()
        self.tool_server = tool_server
        self.memory_store = deps.memory_store or MemoryStore(self.settings.data_dir)
        self.kb_store = deps.kb_store
        self.tasks_store = deps.tasks
        agent_dir = self.settings.data_dir / "agent"
        self.workspace = agent_dir / "workspace"
        self.workspaces = agent_dir / "workspaces"
        config_dir = agent_dir / "config"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workspaces.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        # 会话记录的读写都以这里为根：本进程读历史时查环境变量，子进程按继承的环境写入，
        # 两者必须指向同一目录，否则重启后读不到会话。
        os.environ[CONFIG_DIR_ENV] = str(config_dir)
        self.tools = build_tools(replace(deps, memory_store=self.memory_store))
        # 后台记忆回顾的一次性会话用回顾工具集（按行锚点编辑，不能追问），不与前台工具混在一起。
        self.review_tools = build_tools(
            replace(deps, memory_store=self.memory_store), registry=review_registry
        )
        # 每轮记忆判断的一次性会话用判断工具集：新增、替换、停止使用与追问。
        self.judge_tools = build_tools(
            replace(deps, memory_store=self.memory_store), registry=judge_registry
        )
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

    async def review_memory(self, task_id: str, instructions: str, transcript: str) -> list[dict]:
        """一次性记忆回顾：带回顾工具集，不接续会话，也不进入任何任务历史。

        返回按调用顺序记录的工具调用与结果，整理提示由调用方按这些真实记录生成。
        """
        return await self._memory_session(self.review_tools, task_id, instructions, transcript)

    async def judge_memory(self, task_id: str, instructions: str, message: str) -> list[dict]:
        """一次性记忆判断：带判断工具集，不接续会话；返回按调用顺序记录的工具调用与结果。

        判断对用户可见的提示由调用方按这些真实记录生成，不使用模型的文本回复。
        """
        return await self._memory_session(self.judge_tools, task_id, instructions, message)

    async def _memory_session(
        self, definitions: list[ToolDefinition], task_id: str, instructions: str, message: str
    ) -> list[dict]:
        records: list[dict] = []
        tools = [_recording(definition, records) for definition in definitions]
        # 记忆工具不发草稿事件，队列恒为空，仅为满足端点签名传入。
        queued: asyncio.Queue = asyncio.Queue()
        async with self.tool_server.serve(tools, task_id=task_id, queued=queued) as path:
            task = self.tasks_store.get_task(task_id)
            options = self._oneshot_options(
                instructions, path, tools, task_id=task_id, model=task["model"]
            )
            async with QoderSDKClient(options) as client:
                await client.query(message)
                async for reply in client.receive_response():
                    if isinstance(reply, ResultMessage):
                        if reply.is_error:
                            detail = (reply.result or "").strip() or MODEL_ERROR_MESSAGE
                            raise AgentProtocolError(detail)
                        return records
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
                await client.query(self._query_input(turn))
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
                        # 模型给出完整的工具调用时、执行开始之前告诉页面这一步在做什么。
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                text = self._activity_text(block, visible)
                                if text:
                                    yield {"type": "activity", "text": text}
        while not queued.empty():
            yield queued.get_nowait()
        yield {"type": "error", "message": NO_TERMINAL_MESSAGE}

    @staticmethod
    def _activity_text(block: ToolUseBlock, visible: list[ToolDefinition]) -> str | None:
        """把一次工具调用换成用户能读懂的步骤说明；没有声明说明的工具不展示。"""
        arguments = block.input if isinstance(block.input, dict) else {}
        prefix = f"mcp__{TOOL_SERVER_NAME}__"
        if block.name.startswith(prefix):
            name = block.name[len(prefix) :]
            definition = next((tool for tool in visible if tool.name == name), None)
            if definition is None or definition.activity_renderer is None:
                return None
            try:
                return definition.activity_renderer(arguments)
            except Exception:
                logger.exception("工具 %s 的步骤说明生成失败", name)
                return None
        if block.name in WEB_TOOL_ACTIVITIES:
            label, field = WEB_TOOL_ACTIVITIES[block.name]
            return activity(label, arguments.get(field))
        return None

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

    def _catalog_materials(self) -> tuple[context.Material, ...]:
        """常驻的资料目录：记忆全文之后、本轮材料之前。读不出来时本轮不带目录，不中断调用。"""
        if self.kb_store is None:
            return ()
        try:
            catalog = self.kb_store.catalog()
        except Exception:
            logger.exception("资料目录生成失败，本轮不注入")
            return ()
        return (context.Material(CATALOG_TITLE, catalog),) if catalog else ()

    async def _image_input(self, turn: Turn) -> AsyncIterator[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": turn.message}]
        for attachment in turn.attachments:
            if not attachment.is_image:
                continue
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": attachment.mime_type,
                        "data": base64.b64encode(attachment.path.read_bytes()).decode("ascii"),
                    },
                }
            )
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    def _query_input(self, turn: Turn) -> str | AsyncIterator[dict[str, Any]]:
        if any(item.is_image for item in turn.attachments):
            return self._image_input(turn)
        return turn.message

    def _task_workspace(self, task_id: str) -> Any:
        workspace = self.workspaces / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _permission_callback(self, task_id: str, *, web_enabled: bool, read_enabled: bool):
        workspace = self._task_workspace(task_id).resolve()

        async def authorize(tool_name: str, tool_input: dict, _context: Any):
            if web_enabled and tool_name in WEB_TOOLS:
                return PermissionResultAllow()
            if (
                read_enabled
                and tool_name == "Read"
                and isinstance(tool_input.get("file_path"), str)
            ):
                requested = Path(tool_input["file_path"])
                requested = requested if requested.is_absolute() else workspace / requested
                try:
                    requested.resolve().relative_to(workspace)
                except ValueError:
                    return PermissionResultDeny(message="只能读取当前任务的附件")
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"未授权的工具：{tool_name}")

        return authorize

    def _options(
        self, turn: Turn, *, visible: list[ToolDefinition], path: str
    ) -> QoderAgentOptions:
        snapshot = self.memory_store.snapshot()
        memory_materials = tuple(
            context.Material(title, snapshot[target]["content"])
            for target, title in (("user", "关于你"), ("memory", "事实与约定"))
            if snapshot[target]["content"]
        )
        ctx = context.assemble(
            materials=(*memory_materials, *self._catalog_materials(), *turn.materials)
        )
        workspace = self._task_workspace(turn.task_id)
        web_tools = list(WEB_TOOLS) if turn.kind is TurnKind.MESSAGE else []
        read_enabled = turn.kind is TurnKind.MESSAGE and (workspace / "attachments").is_dir()
        builtins = [*web_tools, *(["Read"] if read_enabled else [])]
        return QoderAgentOptions(
            # 内置工具只开放联网查询（见 WEB_TOOLS），本机设置一律关闭：模型能看到的其余
            # 工具只有本轮 MCP 端点里的那些。
            tools=builtins,
            allowed_tools=[
                *builtins,
                *(f"mcp__{TOOL_SERVER_NAME}__{definition.name}" for definition in visible),
            ],
            can_use_tool=(
                self._permission_callback(
                    turn.task_id, web_enabled=bool(web_tools), read_enabled=read_enabled
                )
                if builtins
                else None
            ),
            mcp_servers={
                TOOL_SERVER_NAME: {"type": "http", "url": self._tool_url(path)},
            },
            allowed_mcp_server_names=[TOOL_SERVER_NAME],
            strict_mcp_config=True,
            setting_sources=[],
            skills=ctx.skills,
            system_prompt=ctx.system_prompt,
            hooks=turn_context_hooks(ctx.additional_context),
            cwd=workspace,
            resume=turn.sdk_session_id,
            include_partial_messages=True,
            auth=self._auth(),
            **self._model_options(selected_model=turn.model),
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

    def _oneshot_options(
        self,
        instructions: str,
        path: str,
        tools: list[ToolDefinition],
        *,
        task_id: str,
        model: str,
    ) -> QoderAgentOptions:
        # 与标题生成同形，但经本轮 MCP 端点带上指定工具集；无 resume，每次都是全新会话。
        return QoderAgentOptions(
            tools=[],
            allowed_tools=[f"mcp__{TOOL_SERVER_NAME}__{definition.name}" for definition in tools],
            mcp_servers={
                TOOL_SERVER_NAME: {"type": "http", "url": self._tool_url(path)},
            },
            allowed_mcp_server_names=[TOOL_SERVER_NAME],
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            system_prompt=instructions,
            cwd=self._task_workspace(task_id),
            include_partial_messages=False,
            auth=self._auth(),
            **self._model_options(selected_model=model),
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

    def _model_options(
        self, *, selected_model: str | None = None, hosted_model: str | None = None
    ) -> dict[str, Any]:
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
        return {"model": hosted_model or selected_model or settings.qoder_model}


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
