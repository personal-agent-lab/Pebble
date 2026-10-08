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
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from functools import partial
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
from server.agent.materials import SessionMaterials
from server.agent.mcp import LOOPBACK_HOST, TOOL_ERROR_MESSAGE, TOOL_SERVER_NAME, ToolServer
from server.agent.prompt import TITLE_PROMPT
from server.agent.toolset import (
    ToolDeps,
    TurnKind,
    build_tools,
    exposed_tools,
)
from server.config import Settings, get_settings
from server.db import session, write
from server.errors import DependencyUnavailableError, error_details
from server.gateway.agent_contract import AgentEvent, AgentProtocolError, Turn
from server.memory.service import MemoryStore
from server.sessions import timeline
from server.sessions.observations import TurnObserver
from server.sessions.tool_trace import begin_tool_call, finish_tool_call
from server.tools.memory.tools import judge_registry, review_registry
from server.tools.personal_kb.catalog import CATALOG_TITLE
from server.tools.registry import Effect, ToolDefinition, ToolPolicy, ToolRegistry, activity

logger = logging.getLogger(__name__)

CONFIG_DIR_ENV = "QODERCN_CONFIG_DIR"

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


def tool_trace_hooks(
    task_id: str,
    run_id: str,
    builtins: set[str],
    on_timeline_change: Callable[[], None] | None = None,
):
    """仅记录本轮开放的 SDK 内置工具；Pebble MCP 由自身端点记录。"""

    async def capture(data, _tool_use_id, _hook_context):
        name = data.get("tool_name")
        call_id = data.get("tool_use_id")
        arguments = data.get("tool_input")
        if name not in builtins or not isinstance(call_id, str):
            return {}
        arguments = arguments if isinstance(arguments, dict) else {}
        event = data.get("hook_event_name")
        if event == "PreToolUse":
            await begin_tool_call(
                task_id=task_id,
                run_id=run_id,
                tool_call_id=call_id,
                name=name,
                arguments=arguments,
                source="builtin",
            )
            if on_timeline_change is not None:
                on_timeline_change()
        elif event in ("PostToolUse", "PostToolUseFailure"):
            if event == "PostToolUse":
                response = data.get("tool_response")
                result = (
                    response
                    if isinstance(response, str)
                    else json.dumps(response, ensure_ascii=False, default=str)
                )
                status = "ok"
            else:
                result = str(data.get("error") or "工具执行失败")
                status = "error"
            await finish_tool_call(
                task_id=task_id,
                run_id=run_id,
                tool_call_id=call_id,
                name=name,
                arguments=arguments,
                status=status,
                result=result,
                source="builtin",
            )
            if on_timeline_change is not None:
                on_timeline_change()
        return {}

    return {
        event: [HookMatcher(hooks=[capture])]
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure")
    }


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
        self.skills_store = deps.skills
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
        # 每轮记忆判断的一次性会话用判断工具集：新增、替换与停止使用。
        self.judge_tools = build_tools(
            replace(deps, memory_store=self.memory_store), registry=judge_registry
        )
        self._check_model_config()
        # 每个进行中调用的 SDK 中断句柄：调度层请求终止时按调用标识找到仍在
        # 会话里的客户端；轮次结束即摘除，句柄只在本进程内存中。
        self._interrupts: dict[str, Callable[[], Any]] = {}

    # ---------- 输入入口 ----------

    def stream_turn(self, turn: Turn) -> AsyncIterator[AgentEvent]:
        """执行一轮调用；消息与材料由调用方组装，网关不区分触发来源。"""
        return self._stream(turn)

    async def interrupt_turn(self, run_id: str) -> bool:
        """请求终止该调用轮正在进行中的 SDK 调用；没有可终止的调用时返回 False。"""
        interrupt = self._interrupts.pop(run_id, None)
        if interrupt is None:
            return False
        await interrupt()
        return True

    async def generate_title(self, text: str) -> str:
        """一次性标题生成：无工具、不接续会话，也不进入任务的对话历史。"""
        return await self.generate_text(TITLE_PROMPT, text)

    async def generate_text(self, instructions: str, text: str) -> str:
        """一次性短文本生成：指令即系统提示，走轻量模型，无工具、不接续会话。"""
        return await self._light_reply(instructions, text)

    async def describe_image(self, instructions: str, data: bytes, mime_type: str) -> str:
        """一次性看图生成文字：与 generate_text 同形，只是输入换成一张图片。"""

        async def image() -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64.b64encode(data).decode("ascii"),
                            },
                        }
                    ],
                },
                "parent_tool_use_id": None,
            }

        return await self._light_reply(instructions, image(), vision=True)

    async def _light_reply(
        self,
        instructions: str,
        prompt: str | AsyncIterator[dict[str, Any]],
        *,
        vision: bool = False,
    ) -> str:
        parts: list[str] = []
        async with QoderSDKClient(self._light_options(instructions, vision=vision)) as client:
            await client.query(prompt)
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
        return await self._memory_session(
            self.review_tools, task_id, instructions, transcript, session_scope="memory_review"
        )

    async def judge_memory(self, task_id: str, instructions: str, message: str) -> list[dict]:
        """一次性记忆判断：带判断工具集，不接续会话；返回按调用顺序记录的工具调用与结果。

        判断对用户可见的提示由调用方按这些真实记录生成，不使用模型的文本回复。
        """
        return await self._memory_session(
            self.judge_tools, task_id, instructions, message, session_scope="memory_judge"
        )

    async def review_skills(
        self,
        review_id: str,
        anchor_task_id: str,
        instructions: str,
        material: str,
        model: str,
        evidence_refs: dict[str, str],
    ) -> list[dict]:
        """一次性 Skill 复盘：只收集候选，模型正常结束后由调度器落库并应用。"""
        from server.skills.review import resolve_evidence_refs, validate_review_reason
        from server.skills.tools import skill_list, skill_view

        if self.skills_store is None:
            raise DependencyUnavailableError("Skill 服务未接入")
        candidates: list[dict] = []
        read_revisions: dict[str, str] = {}
        invalid_candidate_since_last_candidate = False

        def view(skill_id: str, file_path: str | None = None) -> dict:
            result = skill_view.func(skill_id, file_path, skills=self.skills_store)
            read_revisions[skill_id] = result["revision"]
            return result

        def propose(
            action: str,
            payload: dict,
            reason: str,
            evidence_refs_to_use: list[str],
            expected_revision: str | None = None,
        ) -> dict:
            """提出 Skill 创建或修改候选；必须给出轨迹短编号与理由。"""
            from server.skills.models import ChangeAction

            parsed = ChangeAction(action)
            nonlocal invalid_candidate_since_last_candidate
            try:
                validate_review_reason(reason, evidence_refs)
                evidence_item_ids = resolve_evidence_refs(evidence_refs_to_use, evidence_refs)
            except ValueError:
                invalid_candidate_since_last_candidate = True
                raise
            skill_id = payload.get("skill_id")
            if parsed is not ChangeAction.CREATE and (
                not skill_id or read_revisions.get(skill_id) != expected_revision
            ):
                raise ValueError("修改前必须在本次复盘中重新读取目标 Skill 的当前版本")
            candidates.append(
                {
                    "action": action,
                    "payload": payload,
                    "reason": reason,
                    "evidence_item_ids": evidence_item_ids,
                    "expected_revision": expected_revision,
                }
            )
            invalid_candidate_since_last_candidate = False
            return {"status": "staged", "ordinal": len(candidates) - 1}

        registry = ToolRegistry(session_scope="skill_review")
        registry.register(
            skill_list.func,
            name="skill_list",
            description=skill_list.description,
            effect=Effect.READ_ONLY,
            policy=ToolPolicy.DEDICATED_SESSION_ONLY,
        )
        registry.register(
            view,
            name="skill_view",
            description=skill_view.description,
            effect=Effect.READ_ONLY,
            policy=ToolPolicy.DEDICATED_SESSION_ONLY,
        )
        registry.register(
            propose,
            name="skill_propose_change",
            description=(
                "仅提出后台复盘候选，不立即写入。action 为 create/patch/write_file/remove_file。"
                "create 的 payload 含 skill_id、name、description、body；"
                "patch 含 skill_id 与 body 或 old_string/new_string；附件操作含 skill_id、"
                "relative_path 及可选 content。修改现有技能先用 skill_view 读取，"
                "将返回的 revision 传给 expected_revision。reason 是直接展示给用户的修改理由，"
                "用自然语言说明纠正、验证结果和可复用做法，不写 E1 等内部编号或 ID。"
                "evidence_refs_to_use 单独填写轨迹条目前的短编号（如 E1），不要填写长 ID。"
            ),
            effect=Effect.LOCAL_WRITE,
            policy=ToolPolicy.DEDICATED_SESSION_ONLY,
        )
        tools = [
            replace(definition, func=partial(definition.func, skills=self.skills_store))
            if definition.name == "skill_list"
            else definition
            for definition in registry.list_tools()
        ]
        tools = exposed_tools(tools, session_scope="skill_review")
        queued: asyncio.Queue = asyncio.Queue()
        async with self.tool_server.serve(tools, task_id=anchor_task_id, queued=queued) as path:
            options = self._oneshot_options(
                instructions, path, tools, task_id=anchor_task_id, model=model
            )
            async with QoderSDKClient(options) as client:
                await client.query(material)
                async for reply in client.receive_response():
                    if isinstance(reply, ResultMessage):
                        if reply.is_error:
                            raise AgentProtocolError(
                                (reply.result or "").strip() or MODEL_ERROR_MESSAGE
                            )
                        if invalid_candidate_since_last_candidate:
                            raise AgentProtocolError("复盘候选的依据或用户可见理由无效，尚未纠正")
                        return candidates
        raise AgentProtocolError(NO_TERMINAL_MESSAGE)

    async def _memory_session(
        self,
        definitions: list[ToolDefinition],
        task_id: str,
        instructions: str,
        message: str,
        *,
        session_scope: str,
    ) -> list[dict]:
        records: list[dict] = []
        visible = exposed_tools(definitions, session_scope=session_scope)
        tools = [_recording(definition, records) for definition in visible]
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
        from server.skills.runtime import turn_scope

        with turn_scope(
            task_id=turn.task_id,
            run_id=turn.run_id,
            skills=turn.skills,
            excluded_skill_ids=turn.excluded_skill_ids,
            auto_match=turn.auto_match,
        ):
            async for event in self._scoped_stream(turn):
                yield event

    async def _scoped_stream(self, turn: Turn) -> AsyncIterator[AgentEvent]:
        queued: asyncio.Queue[AgentEvent] = asyncio.Queue()
        visible = exposed_tools(self.tools, kind=turn.kind)
        announced: str | None = None
        streamed = False
        terminal: AgentEvent | None = None
        observer = TurnObserver(turn.run_id)
        materials = SessionMaterials(
            self._task_workspace(turn.task_id) / ".context-materials.json", turn.sdk_session_id
        )
        materials.begin()
        async with self.tool_server.serve(
            visible,
            task_id=turn.task_id,
            target_operation_id=turn.target_operation_id,
            queued=queued,
            run_id=turn.run_id,
            on_timeline_change=turn.on_timeline_change,
        ) as path:
            options = self._options(
                turn, visible=visible, path=path, observer=observer, session_materials=materials
            )
            async with QoderSDKClient(options) as client:
                if turn.run_id is not None:
                    self._interrupts[turn.run_id] = client.interrupt
                try:
                    context_before: dict | None = None
                    if turn.sdk_session_id is not None:
                        context_before = await self._compact_if_needed(client, observer)
                    else:
                        # 新会话首轮没有可压缩的上下文，但轮前读数每轮都要记。
                        context_before = await self._read_context_before(client, observer)
                    await client.query(self._query_input(turn))
                    async for message in client.receive_response():
                        # 工具事件在产生它的那次调用之后、模型的下一条消息之前送出。
                        while not queued.empty():
                            yield queued.get_nowait()
                        if isinstance(message, ResultMessage):
                            observer.record_result(message)
                            await self._record_context_after(client, observer)
                            announced = announced or turn.sdk_session_id or message.session_id
                            terminal = _result_event(message)
                            break
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            session_id = message.data.get("session_id")
                            # 同一会话一轮里会上报多次：只广播第一次，标识真的变化时照常上报。
                            if session_id and session_id != announced:
                                announced = session_id
                                yield {"type": "session", "sdk_session_id": session_id}
                        elif (
                            isinstance(message, SystemMessage)
                            and message.subtype == "compact_boundary"
                        ):
                            # SDK 自动压缩的真实边界信号；没有边界不声称发生过压缩。
                            observer.record_compact(auto=True, before=context_before)
                        elif isinstance(message, StreamEvent):
                            text = _delta_text(message.event)
                            if text:
                                streamed = True
                                yield {"type": "text", "text": text}
                        elif isinstance(message, AssistantMessage):
                            observer.collect_usage(message)
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
                finally:
                    self._interrupts.pop(turn.run_id, None)
        while not queued.empty():
            yield queued.get_nowait()
        if terminal is not None:
            # SDK 客户端正常退出、会话持久化结束后才留下检查点；退出失败也不能冒充送达。
            if terminal["type"] == "done":
                try:
                    materials.commit(announced)
                except OSError:
                    logger.exception("会话材料摘要保存失败，下轮重新加载背景")
            yield terminal
            return
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

    async def _read_context_before(
        self, client: QoderSDKClient, observer: TurnObserver
    ) -> dict | None:
        """轮首读取上下文占用并记录轮前读数——每轮都记，含新会话首轮。

        读取失败只留空值；返回读数（观测存储形状）供压缩判断与压缩步骤引用。
        """
        try:
            usage = await client.get_context_usage()
        except Exception:
            logger.exception("轮首的上下文占用读取失败")
            return None
        reading = _context_reading(usage)
        if reading is not None:
            observer.record_context(before=reading)
        return reading

    async def _compact_if_needed(
        self, client: QoderSDKClient, observer: TurnObserver
    ) -> dict | None:
        """运行时未启用自动压缩时，在达到其阈值后先完成手动压缩。

        返回轮首的上下文读数供压缩步骤引用；读取失败只留空值。
        """
        context_before = await self._read_context_before(client, observer)
        if context_before is None:
            return None
        used = context_before.get("used_percentage")
        threshold = context_before.get("threshold_percentage")
        if context_before.get("auto_compact_enabled") or used is None or threshold is None:
            return context_before
        if used < threshold:
            return context_before

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
        after = None
        try:
            after = _context_reading(await client.get_context_usage())
        except Exception:
            logger.exception("手动压缩后的上下文占用读取失败")
        observer.record_compact(auto=False, before=context_before, after=after)
        return context_before

    async def _record_context_after(self, client: QoderSDKClient, observer: TurnObserver) -> None:
        """轮末读取上下文占用并回填压缩步骤的后占用；读取失败只留空值。"""
        try:
            usage = await client.get_context_usage()
        except Exception:
            logger.exception("轮末的上下文占用读取失败")
            return
        observer.record_context(after=_context_reading(usage))

    def _catalog_materials(
        self, observer: TurnObserver | None = None
    ) -> tuple[context.Material, ...]:
        """常驻的资料目录：记忆全文之后、本轮材料之前。读不出来时本轮不带目录，不中断调用。"""
        if self.kb_store is None:
            return ()
        try:
            catalog = self.kb_store.catalog()
        except Exception:
            logger.exception("资料目录生成失败，本轮不注入")
            if observer is not None:
                observer.degraded(
                    "catalog_failed",
                    {"catalog": "kb"},
                    skipped={"category": "资料目录", "reason": "生成失败"},
                )
            return ()
        return (context.Material(CATALOG_TITLE, catalog),) if catalog else ()

    def _query_input(self, turn: Turn) -> str:
        return turn.message

    def _task_workspace(self, task_id: str) -> Any:
        workspace = self.workspaces / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _permission_callback(
        self,
        task_id: str,
        run_id: str,
        *,
        web_enabled: bool,
        read_enabled: bool,
        allowed_mcp_tools: frozenset[str] = frozenset(),
        on_timeline_change: Callable[[], None] | None = None,
    ):
        workspace = self._task_workspace(task_id).resolve()

        async def authorize(tool_name: str, tool_input: dict, permission_context: Any):
            if tool_name in allowed_mcp_tools:
                return PermissionResultAllow()
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
                    await record_denial(
                        tool_name, tool_input, permission_context, "只能读取当前任务的附件"
                    )
                    return PermissionResultDeny(message="只能读取当前任务的附件")
                return PermissionResultAllow()
            reason = f"未授权的工具：{tool_name}"
            await record_denial(tool_name, tool_input, permission_context, reason)
            return PermissionResultDeny(message=reason)

        async def record_denial(
            tool_name: str, tool_input: dict, permission_context: Any, reason: str
        ):
            # SDK 只保证实际进入权限回调的请求可见；没有调用 ID 时等失败 hook。
            call_id = getattr(permission_context, "tool_use_id", None)
            if not isinstance(call_id, str):
                return
            if tool_name not in {*WEB_TOOLS, "Read"}:
                return
            await finish_tool_call(
                task_id=task_id,
                run_id=run_id,
                tool_call_id=call_id,
                name=tool_name,
                arguments=tool_input,
                status="error",
                result=reason,
                source="builtin",
                denied=True,
            )
            if on_timeline_change is not None:
                on_timeline_change()

        return authorize

    def _options(
        self,
        turn: Turn,
        *,
        visible: list[ToolDefinition],
        path: str,
        observer: TurnObserver | None = None,
        session_materials: SessionMaterials | None = None,
    ) -> QoderAgentOptions:
        selected_materials = []
        skill_catalog = []
        if self.skills_store is not None:
            from server.skills import runtime as skills_runtime

            def notify_skill_skip(text: str) -> None:
                # 装配层降级的用户告知：程序写时间线通知并让页面即时可见（skills.md §9）；
                # 文案由技能域给出，这里只负责投递。写入失败不影响这一轮对话。
                try:
                    with session(turn.db_path) as conn, write(conn):
                        timeline.insert_notice(conn, turn.task_id, turn.run_id, text)
                except Exception:
                    logger.exception("手动技能降级的时间线通知写入失败")
                if turn.on_timeline_change is not None:
                    turn.on_timeline_change()

            selected_materials.extend(
                skills_runtime.manual_materials(
                    self.skills_store,
                    turn.skills,
                    task_id=turn.task_id,
                    run_id=turn.run_id,
                    observer=observer,
                    notify=notify_skill_skip,
                )
            )
            if turn.auto_match:
                catalog = skills_runtime.catalog_material(
                    self.skills_store,
                    excluded_skill_ids=turn.excluded_skill_ids,
                    observer=observer,
                )
                if catalog is not None:
                    skill_catalog.append(catalog)
        snapshot = self.memory_store.snapshot()
        memory_materials = tuple(
            context.Material(title, snapshot[target]["content"])
            for target, title in (("user", "关于你"), ("memory", "事实与约定"))
            if snapshot[target]["content"]
        )
        kb_materials = self._catalog_materials(observer)
        background = (*memory_materials, *kb_materials, *skill_catalog)
        ctx = context.assemble()
        if observer is not None:
            # 钩子实际提交时再记录；复用的背景不冒充本轮新增材料。
            observer.record_materials([])
        workspace = self._task_workspace(turn.task_id)
        web_tools = list(WEB_TOOLS) if turn.kind is TurnKind.MESSAGE else []
        read_enabled = turn.kind is TurnKind.MESSAGE and bool(turn.attachments)
        builtins = [*web_tools, *(["Read"] if read_enabled else [])]
        session_materials = session_materials or SessionMaterials(
            workspace / ".context-materials.json", turn.sdk_session_id
        )
        session_materials.configure(
            background=background,
            selected=selected_materials,
            events=turn.materials,
            observe=observer.record_materials if observer is not None else None,
        )
        hooks = session_materials.hooks()
        hooks.update(
            tool_trace_hooks(turn.task_id, turn.run_id, set(builtins), turn.on_timeline_change)
        )
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
                    turn.task_id,
                    turn.run_id,
                    web_enabled=bool(web_tools),
                    read_enabled=read_enabled,
                    allowed_mcp_tools=frozenset(
                        f"mcp__{TOOL_SERVER_NAME}__{definition.name}" for definition in visible
                    ),
                    on_timeline_change=turn.on_timeline_change,
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
            hooks=hooks,
            cwd=workspace,
            resume=turn.sdk_session_id,
            include_partial_messages=True,
            auth=self._auth(),
            model=turn.model or self.settings.qoder_model,
        )

    def _light_options(self, instructions: str, *, vision: bool = False) -> QoderAgentOptions:
        return QoderAgentOptions(
            tools=[],
            allowed_tools=[],
            mcp_servers={},
            allowed_mcp_server_names=[],
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            system_prompt=instructions,
            cwd=self.workspace,
            include_partial_messages=False,
            auth=self._auth(),
            **self._light_model_options(vision=vision),
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
            model=model or self.settings.qoder_model,
        )

    def _tool_url(self, path: str) -> str:
        return f"http://{LOOPBACK_HOST}:{self.settings.tool_port}{path}"

    def _auth(self) -> Any:
        token = self.settings.qoder_token
        if token is None:
            raise DependencyUnavailableError("未配置 Qoder 访问令牌")
        return access_token(token.get_secret_value())

    def _check_model_config(self) -> None:
        """轻量模型三项要么齐全且供应商已登记，要么都不配，缺项或写错都在装配期报错。

        只配一部分就静默退回托管模型，会让调用方以为在用自己的账号与额度，
        实际请求却去了别处。
        """
        settings = self.settings
        values = (
            ("PEBBLE_LIGHT_MODEL_PROVIDER", settings.light_model_provider),
            ("PEBBLE_LIGHT_MODEL_API_KEY", settings.light_model_api_key),
            ("PEBBLE_LIGHT_MODEL", settings.light_model),
        )
        if not any(value for _, value in values):
            return
        missing = [name for name, value in values if not value]
        if missing:
            raise DependencyUnavailableError(f"轻量模型配置不完整，缺少：{', '.join(missing)}")
        if settings.light_model_provider not in BYOK_PROVIDERS:
            raise DependencyUnavailableError(
                f"未登记的模型供应商：{settings.light_model_provider}，"
                f"可选：{', '.join(sorted(BYOK_PROVIDERS))}"
            )

    def _light_model_options(self, *, vision: bool = False) -> dict[str, Any]:
        """配了轻量模型就转成 BYOK 的 `resolve_model`，凭证只在这里读取；否则沿用托管型号。

        看图调用要声明 `is_vl`：不声明时 CLI 按纯文本模型处理，图片不会发给模型。
        模型本身不支持图片时这次调用失败或回答看不到，由调用方按空说明处理。
        """
        settings = self.settings
        if not settings.light_model_provider:
            return {"model": settings.qoder_model}
        custom: dict[str, Any] = {
            "provider": settings.light_model_provider,
            "model": settings.light_model,
            "api_key": settings.light_model_api_key.get_secret_value(),
            "style": BYOK_STYLE,
        }
        if vision:
            custom["is_vl"] = True
        if settings.light_model_base_url:
            custom["url"] = settings.light_model_base_url
        return {"resolve_model": lambda _context: {"model": custom}}


def _context_reading(usage: dict | None) -> dict | None:
    """把 get_context_usage() 的读数压成观测存储的形状；读不到的字段留空。

    类别分解就是 CLI `/context` 视图的同款数据：每类一个占窗口百分比，运行时
    不给绝对 token 数（分词在服务端），所以这里也只存百分比。类别名与顺序照抄，
    界面按自己的措辞显示。
    """
    if not isinstance(usage, dict):
        return None
    context_window = usage.get("contextWindow") or {}
    automatic = usage.get("autoCompact") or {}
    categories = [
        {"kind": entry.get("type"), "percentage": entry.get("percentage")}
        for entry in usage.get("categories") or []
        if isinstance(entry, dict) and isinstance(entry.get("type"), str)
    ]
    return {
        "used_percentage": context_window.get("usedPercentage"),
        "threshold_percentage": automatic.get("thresholdPercentage"),
        "auto_compact_enabled": bool(automatic.get("enabled")),
        "categories": categories,
    }


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
