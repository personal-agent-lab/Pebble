"""SDK 网关：选项装配、事件映射、工具边界与会话历史读取。

只替换 SDK 客户端本身：工具经进程内 MCP 端点按真实协议调用，存储与草稿都是真实实现。
"""

import asyncio
import json
from dataclasses import dataclass, replace

import pytest
from qodercn_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from server.agent import client as agent_client
from server.agent.client import BYOK_PROVIDERS, QoderGateway
from server.agent.context import Material, assemble
from server.agent.materials import FULL_NOTE
from server.agent.mcp import MCP_MOUNT_PATH, TOOL_SERVER_NAME, ToolServer
from server.agent.prompt import BASE_PROMPT
from server.agent.toolset import ToolDeps, TurnKind, exposed_tools
from server.config import Settings
from server.db import init_db
from server.errors import DependencyUnavailableError
from server.failures import FailureError
from server.gateway.agent_contract import AgentProtocolError, Turn, TurnAttachment
from server.gateway.runtime import execution_result_content
from server.sessions.service import SessionStore
from server.skills.service import SkillService
from server.tools.gmail.service import MailDraftStore
from server.tools.gmail.trigger import new_mail_content
from server.tools.personal_kb.service import KbStore
from server.tools.registry import Effect, ToolDefinition, ToolPolicy
from tests.support import seed_memory
from tests.support.gmail_double import MockGmailClient
from tests.support.mcp_http import mcp_session, tool_payload

DRAFT = {
    "source_message_id": "msg_invite_001",
    "to": ["alice@example.com"],
    "subject": "Re: 邀请",
    "body": "谢谢邀请，我准时参加。",
}
TURN_TOKEN = "0123456789abcdef"


def configured(settings, **overrides) -> Settings:
    return Settings(
        data_dir=settings.data_dir,
        QODERCN_PERSONAL_ACCESS_TOKEN="test-token",
        _env_file=None,
        **overrides,
    )


def make_gateway(settings, *, gmail=None, **overrides) -> QoderGateway:
    init_db()
    return QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(),
            tasks=SessionStore(),
            gmail=gmail or MockGmailClient(),
        ),
        ToolServer(),
        settings=configured(settings, **overrides),
    )


def options_for(gateway: QoderGateway, kind: TurnKind, **turn_fields):
    turn = Turn(
        kind=kind,
        task_id=turn_fields.pop("task_id", "task-1"),
        sdk_session_id=turn_fields.pop("sdk_session_id", None),
        message="测试输入",
        **turn_fields,
    )
    return options_for_turn(gateway, turn)


def options_for_turn(gateway: QoderGateway, turn: Turn):
    visible = exposed_tools(gateway.tools, kind=turn.kind)
    return gateway._options(turn, visible=visible, path=f"{MCP_MOUNT_PATH}/{TURN_TOKEN}")


def injected_context(options) -> str:
    async def inject():
        blocks = []
        for event, data in (
            ("SessionStart", {"source": "startup"}),
            ("UserPromptSubmit", {"prompt": "测试输入"}),
        ):
            result = await options.hooks[event][0].hooks[0](data, None, {})
            blocks.append(result.get("hookSpecificOutput", {}).get("additionalContext", ""))
        return "\n\n".join(block for block in blocks if block)

    return asyncio.run(inject())


def message_turn(message: str, *, task_id: str = "task-1") -> Turn:
    return Turn(kind=TurnKind.MESSAGE, task_id=task_id, sdk_session_id=None, message=message)


async def collect(stream):
    return [event async for event in stream]


# ---------- 选项装配 ----------


def test_new_mail_turn_sees_only_readonly_tools(settings):
    gateway = make_gateway(settings)
    options = options_for(gateway, TurnKind.NEW_MAIL)

    readonly = exposed_tools(gateway.tools, kind=TurnKind.NEW_MAIL)
    assert options.allowed_tools == [f"mcp__pebble__{tool.name}" for tool in readonly]
    visible = {name.rsplit("__", 1)[-1] for name in options.allowed_tools}
    assert not {"gmail_prepare_reply", "gmail_update_draft"} & visible
    assert "memory" not in visible
    # 联网查询与本机设置关闭：系统输入的轮次模型只能连本轮登记的 MCP 端点。
    assert options.tools == []
    assert options.setting_sources == []
    assert options.mcp_servers == {
        TOOL_SERVER_NAME: {
            "type": "http",
            "url": f"http://127.0.0.1:{gateway.settings.tool_port}{MCP_MOUNT_PATH}/{TURN_TOKEN}",
        }
    }
    assert options.allowed_mcp_server_names == [TOOL_SERVER_NAME]
    assert options.strict_mcp_config is True
    assert options.skills == []


def test_message_turn_allows_drafting_and_resumes_session(settings):
    gateway = make_gateway(settings)
    options = options_for(gateway, TurnKind.MESSAGE, sdk_session_id="session-1")

    names = {name.rsplit("__", 1)[-1] for name in options.allowed_tools}
    assert "gmail_prepare_reply" in names and "gmail_update_draft" in names
    assert options.resume == "session-1"
    assert options.include_partial_messages is True
    assert options.cwd == gateway.workspaces / "task-1"


@pytest.mark.parametrize("kind", list(TurnKind))
def test_web_tools_are_visible_only_on_user_initiated_turns(settings, kind):
    options = options_for(make_gateway(settings), kind)

    if kind is TurnKind.MESSAGE:
        assert options.tools == ["WebSearch", "WebFetch"]
        assert {"WebSearch", "WebFetch"} <= set(options.allowed_tools)
        assert options.can_use_tool is not None
    else:
        # 触发轮与结果回传轮的输入来自外部内容，不能让其中的指令驱动联网请求。
        assert options.tools == []
        assert not {"WebSearch", "WebFetch"} & set(options.allowed_tools)
        assert options.can_use_tool is None


def test_user_turn_permission_callback_allows_only_declared_web_tools(settings):
    options = options_for(make_gateway(settings), TurnKind.MESSAGE)
    assert options.can_use_tool is not None
    context = ToolPermissionContext()

    fetch = asyncio.run(options.can_use_tool("WebFetch", {"url": "https://example.com"}, context))
    registered = next(name for name in options.allowed_tools if name.startswith("mcp__"))
    mcp = asyncio.run(options.can_use_tool(registered, {}, context))
    unknown_mcp = asyncio.run(options.can_use_tool("mcp__pebble__missing", {}, context))
    unexpected = asyncio.run(options.can_use_tool("Bash", {"command": "true"}, context))

    assert isinstance(fetch, PermissionResultAllow)
    assert isinstance(mcp, PermissionResultAllow)
    assert isinstance(unknown_mcp, PermissionResultDeny)
    assert isinstance(unexpected, PermissionResultDeny)


def test_attachment_turn_enables_read_only_inside_task_workspace(settings):
    gateway = make_gateway(settings)
    workspace = gateway.workspaces / "task-1"
    attachment = workspace / "attachments" / "file-1"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_text("random", encoding="utf-8")
    turn = Turn(
        kind=TurnKind.MESSAGE,
        task_id="task-1",
        sdk_session_id=None,
        message="读取附件",
        attachments=(
            TurnAttachment(
                "file-1", "note.txt", "text/plain", 6, attachment, "attachments/file-1.txt"
            ),
        ),
    )
    options = options_for_turn(gateway, turn)
    context = ToolPermissionContext()

    assert "Read" in options.allowed_tools
    inside = asyncio.run(options.can_use_tool("Read", {"file_path": "attachments/file-1"}, context))
    registered = next(name for name in options.allowed_tools if name.startswith("mcp__"))
    mcp = asyncio.run(options.can_use_tool(registered, {}, context))
    outside = asyncio.run(
        options.can_use_tool("Read", {"file_path": "../task-2/attachments/file-2"}, context)
    )
    shell = asyncio.run(options.can_use_tool("Bash", {"command": "true"}, context))
    assert isinstance(inside, PermissionResultAllow)
    assert isinstance(mcp, PermissionResultAllow)
    assert isinstance(outside, PermissionResultDeny)
    assert isinstance(shell, PermissionResultDeny)


def test_follow_up_without_attachments_denies_read_even_when_workspace_has_files(settings):
    gateway = make_gateway(settings)
    workspace = gateway.workspaces / "task-1" / "attachments"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "old-report.pdf").write_bytes(b"%PDF-1.4")
    options = options_for(gateway, TurnKind.MESSAGE)

    assert "Read" not in options.allowed_tools
    assert "Read" not in options.tools
    denied = asyncio.run(
        options.can_use_tool(
            "Read", {"file_path": "attachments/old-report.pdf"}, ToolPermissionContext()
        )
    )
    assert isinstance(denied, PermissionResultDeny)


def test_image_attachment_uses_text_input_and_workspace_read(settings):
    gateway = make_gateway(settings)
    path = gateway.workspaces / "task-1" / "attachments" / "image-1"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"random-image")
    turn = Turn(
        kind=TurnKind.MESSAGE,
        task_id="task-1",
        sdk_session_id=None,
        message="看图",
        attachments=(
            TurnAttachment(
                "image-1", "image.png", "image/png", 12, path, "attachments/image-1.png"
            ),
        ),
    )

    assert gateway._query_input(turn) == "看图"
    options = options_for_turn(gateway, turn)
    assert "Read" in options.allowed_tools
    assert isinstance(
        asyncio.run(
            options.can_use_tool(
                "Read", {"file_path": "attachments/image-1"}, ToolPermissionContext()
            )
        ),
        PermissionResultAllow,
    )


@pytest.mark.parametrize("kind", list(TurnKind))
def test_memory_tools_are_visible_only_in_user_turns(settings, kind):
    gateway = make_gateway(settings)
    gateway.tools.extend(gateway.review_tools)
    names = {name.rsplit("__", 1)[-1] for name in options_for(gateway, kind).allowed_tools}

    assert ("memory_edit" in names) is (kind is TurnKind.MESSAGE)


def test_review_options_expose_review_tools_in_fresh_session(settings):
    gateway = make_gateway(settings)
    options = gateway._oneshot_options(
        "回顾指令",
        f"{MCP_MOUNT_PATH}/{TURN_TOKEN}",
        gateway.review_tools,
        task_id="task-1",
        model="q-model",
    )

    assert options.allowed_tools == [
        f"mcp__{TOOL_SERVER_NAME}__{name}" for name in ("memory_edit",)
    ]
    assert options.tools == []
    assert options.setting_sources == []
    assert options.mcp_servers == {
        TOOL_SERVER_NAME: {
            "type": "http",
            "url": f"http://127.0.0.1:{gateway.settings.tool_port}{MCP_MOUNT_PATH}/{TURN_TOKEN}",
        }
    }
    assert options.allowed_mcp_server_names == [TOOL_SERVER_NAME]
    assert options.system_prompt == "回顾指令"
    assert options.resume is None
    assert options.hooks is None
    assert options.include_partial_messages is False


def test_external_write_requires_user_turn_policy():
    with pytest.raises(ValueError, match="不允许的工具权限组合"):
        ToolDefinition(
            name="send_now",
            description="",
            func=lambda: None,
            effect=Effect.EXTERNAL_WRITE,
            policy=ToolPolicy.ALL_TURNS,
        )


def test_execution_result_enters_system_prompt(settings):
    gateway = make_gateway(settings)
    message, materials = execution_result_content(
        operation_id="op1",
        version=2,
        result={"status": "sent"},
        confirmed_content={"to": ["laozhou@example.test"], "subject": "回复", "body": "最终正文"},
    )
    turn = Turn(
        kind=TurnKind.EXECUTION_RESULT,
        task_id="task-1",
        sdk_session_id="session-1",
        message=message,
        materials=materials,
    )
    options = options_for_turn(gateway, turn)

    assert options.system_prompt == BASE_PROMPT
    injected = injected_context(options)
    assert '"status": "sent"' in injected
    assert "op1" in injected
    assert "最终正文" in injected
    # 回传材料只进附加上下文，不伪装成用户说过的话。
    assert "op1" not in turn.message
    assert "最终正文" not in turn.message


def test_new_mail_turn_passes_ids_as_material(settings):
    gateway = make_gateway(settings)
    message, materials = new_mail_content(source_message_id="msg-1", thread_id="th-1")
    turn = Turn(
        kind=TurnKind.NEW_MAIL,
        task_id="task-1",
        sdk_session_id=None,
        message=message,
        materials=materials,
    )
    options = options_for_turn(gateway, turn)

    # 标识以 JSON 材料块进附加上下文，模型不必从散文句子里解析。
    assert options.system_prompt == BASE_PROMPT
    injected = injected_context(options)
    assert '"source_message_id": "msg-1"' in injected
    assert '"thread_id": "th-1"' in injected
    assert "msg-1" not in turn.message


def test_materials_render_by_content_type():
    ctx = assemble(materials=[Material("备忘", "周二下午有组会"), Material("载荷", {"a": 1})])

    # 基础提示固定；材料按序注入，自由文本按原文渲染，结构化数据渲染为 JSON 块。
    assert ctx.system_prompt == BASE_PROMPT
    assert "## 备忘\n周二下午有组会" in ctx.additional_context
    assert '## 载荷\n{\n  "a": 1\n}' in ctx.additional_context


def test_resumed_turn_injects_fresh_material_without_changing_base_prompt(settings):
    gateway = make_gateway(settings)
    first = options_for(
        gateway,
        TurnKind.MESSAGE,
        sdk_session_id="session-1",
        materials=(Material("当前生效规则", "回答先给结论"),),
    )
    second = options_for(
        gateway,
        TurnKind.MESSAGE,
        sdk_session_id="session-1",
        materials=(Material("当前生效规则", "回答先解释推导"),),
    )

    assert first.system_prompt == second.system_prompt == BASE_PROMPT
    assert injected_context(first).endswith("## 当前生效规则\n回答先给结论")
    assert injected_context(second).endswith("## 当前生效规则\n回答先解释推导")


def test_memory_is_reloaded_and_precedes_turn_materials(settings):
    gateway = make_gateway(settings)
    seed_memory(gateway.memory_store, "user", "回答先给结论")
    first = options_for(
        gateway,
        TurnKind.MESSAGE,
        materials=(Material("本轮材料", "只对本轮有效"),),
    )
    seed_memory(gateway.memory_store, "user", "回答先解释推导")
    seed_memory(gateway.memory_store, "memory", "Pebble 使用 Python")
    second = options_for(
        gateway,
        TurnKind.MESSAGE,
        sdk_session_id="session-1",
        materials=(Material("本轮材料", "只对本轮有效"),),
    )

    assert "## 关于你\n回答先给结论" in injected_context(first)
    assert injected_context(second) == (
        FULL_NOTE + "\n\n"
        "## 关于你\n回答先解释推导\n\n"
        "## 事实与约定\nPebble 使用 Python\n\n"
        "## 本轮材料\n只对本轮有效"
    )


def test_kb_catalog_is_injected_after_memory_and_before_turn_materials(settings):
    init_db()
    kb = KbStore(settings.data_dir)
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(), tasks=SessionStore(), gmail=MockGmailClient(), kb_store=kb
        ),
        ToolServer(),
        settings=configured(settings),
    )
    seed_memory(gateway.memory_store, "user", "回答先给结论")
    assert injected_context(options_for(gateway, TurnKind.MESSAGE)).endswith(
        "## 关于你\n回答先给结论"
    )

    kb.save(title="星云验收纪要", body="通过。", path="项目/验收", summary="二期验收结论")
    options = options_for(gateway, TurnKind.NEW_MAIL, materials=(Material("本轮材料", "新邮件"),))

    assert injected_context(options) == (
        FULL_NOTE + "\n\n"
        "## 关于你\n回答先给结论\n\n"
        "## 资料目录\n"
        "资料库共 1 份资料；需要细节时用 kb_search 检索，或用 kb_read 读取原文。\n"
        "- 项目/（1 份）\n"
        "  - 星云验收纪要：二期验收结论\n\n"
        "## 本轮材料\n新邮件"
    )


def test_catalog_failure_does_not_break_the_turn(settings, monkeypatch):
    init_db()
    kb = KbStore(settings.data_dir)
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(), tasks=SessionStore(), gmail=MockGmailClient(), kb_store=kb
        ),
        ToolServer(),
        settings=configured(settings),
    )

    def broken():
        raise RuntimeError("模拟读取失败")

    monkeypatch.setattr(kb, "catalog", broken)
    options = options_for(gateway, TurnKind.MESSAGE, materials=(Material("本轮材料", "照常进行"),))
    assert injected_context(options).endswith("## 本轮材料\n照常进行")


def test_turn_without_materials_registers_lifecycle_hooks(settings):
    options = options_for(make_gateway(settings), TurnKind.MESSAGE)

    # 无材料仍保留生命周期钩子，以便清除旧材料、处理压缩与后续更新。
    assert injected_context(options) == FULL_NOTE


def test_manual_compaction_runs_before_resumed_turn_when_sdk_auto_compact_is_off(
    settings, monkeypatch
):
    gateway = make_gateway(settings)
    calls = []

    class CompactingClient:
        def __init__(self, _options):
            self.responses = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_context_usage(self):
            return {
                "contextWindow": {"usedPercentage": 85},
                "autoCompact": {"enabled": False, "thresholdPercentage": 80},
            }

        async def query(self, message):
            calls.append(message)
            if message == "/compact":
                self.responses = [
                    SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "manual"}}),
                    result(),
                ]
            else:
                self.responses = [result()]

        async def receive_response(self):
            for item in self.responses:
                yield item

    monkeypatch.setattr(agent_client, "QoderSDKClient", CompactingClient)
    turn = Turn(
        kind=TurnKind.MESSAGE,
        task_id="task-1",
        sdk_session_id="session-1",
        message="继续任务",
    )

    events = asyncio.run(collect(gateway.stream_turn(turn)))

    assert calls == ["/compact", "继续任务"]
    assert events == [{"type": "done"}]


def test_manual_compaction_is_skipped_below_threshold(settings, monkeypatch):
    gateway = make_gateway(settings)
    calls = []

    class UnpressuredClient:
        def __init__(self, _options):
            self.responses = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_context_usage(self):
            return {
                "contextWindow": {"usedPercentage": 25},
                "autoCompact": {"enabled": False, "thresholdPercentage": 80},
            }

        async def query(self, message):
            calls.append(message)
            self.responses = [result()]

        async def receive_response(self):
            for item in self.responses:
                yield item

    monkeypatch.setattr(agent_client, "QoderSDKClient", UnpressuredClient)
    turn = Turn(
        kind=TurnKind.MESSAGE,
        task_id="task-1",
        sdk_session_id="session-1",
        message="继续任务",
    )

    events = asyncio.run(collect(gateway.stream_turn(turn)))

    assert calls == ["继续任务"]
    assert events == [{"type": "done"}]


def light_gateway(settings, **overrides):
    config = {
        "qoder_model": "q-model",
        "light_model": "deepseek-flash-pg",
        "light_model_provider": "deepseek",
        "light_model_api_key": "sk-test",
    }
    return make_gateway(settings, **(config | overrides))


def test_light_model_serves_short_calls_only(settings):
    """标题与资料说明走自有 API Key 的轻量模型；主对话仍用任务所选的托管型号。"""
    gateway = light_gateway(settings, light_model_base_url="https://api.deepseek.com")
    options = gateway._light_options("起标题")
    assert options.model is None
    assert options.resolve_model(None) == {
        "model": {
            "provider": "deepseek",
            "model": "deepseek-flash-pg",
            "api_key": "sk-test",
            "style": "openai",
            "url": "https://api.deepseek.com",
        }
    }
    assert "sk-test" not in options.system_prompt

    turn = options_for(gateway, TurnKind.MESSAGE, model="q-selected")
    assert turn.model == "q-selected"
    assert turn.resolve_model is None

    hosted = make_gateway(settings, qoder_model="q-model")
    assert hosted._light_options("起标题").model == "q-model"
    assert hosted._light_options("起标题").resolve_model is None


def test_image_description_declares_vision_to_light_model(settings):
    """看图调用要声明 is_vl，否则 CLI 按纯文本模型处理，图片不会发给模型；文本调用不声明。"""
    gateway = light_gateway(settings)
    assert "is_vl" not in gateway._light_options("起标题").resolve_model(None)["model"]
    vision = gateway._light_options("看图", vision=True).resolve_model(None)["model"]
    assert vision["is_vl"] is True


def test_partial_light_model_config_fails_at_assembly(settings):
    """轻量模型只配一部分不能静默退回托管模型：装配期就报错并指出缺项。"""
    with pytest.raises(DependencyUnavailableError, match="PEBBLE_LIGHT_MODEL$"):
        make_gateway(settings, light_model_provider="deepseek", light_model_api_key="sk-test")

    with pytest.raises(DependencyUnavailableError, match="PEBBLE_LIGHT_MODEL_API_KEY"):
        make_gateway(settings, light_model_provider="deepseek", light_model="deepseek-flash-pg")


def test_unregistered_provider_fails_at_assembly(settings):
    """登记表以 `list_byok_providers()` 目录为准，报错时要能看出该填什么。"""
    with pytest.raises(DependencyUnavailableError, match="未登记的模型供应商：qwen") as error:
        light_gateway(settings, light_model_provider="qwen")
    for provider in BYOK_PROVIDERS:
        assert provider in str(error.value)


def test_missing_token_refuses_the_turn(settings):
    init_db()
    gateway = QoderGateway(
        ToolDeps(drafts=MailDraftStore(), tasks=SessionStore(), gmail=MockGmailClient()),
        ToolServer(),
        settings=Settings(data_dir=settings.data_dir, _env_file=None),
    )
    with pytest.raises(DependencyUnavailableError):
        options_for(gateway, TurnKind.MESSAGE)


# ---------- 事件映射 ----------


@dataclass
class ToolCall:
    """脚本里的工具调用：SDK 替身按 MCP 协议执行它，模拟模型调用工具。"""

    name: str
    fields: dict


def install_sdk(monkeypatch, gateway: QoderGateway, script):
    """替换 SDK 客户端：记录装配结果，按脚本产生消息，工具调用打到真实端点。"""
    captured = {"options": None, "message": None, "results": [], "contexts": []}

    class StubClient:
        def __init__(self, options):
            captured["options"] = options

        async def __aenter__(self):
            for matcher in (captured["options"].hooks or {}).get("SessionStart", []):
                for hook in matcher.hooks:
                    output = await hook(
                        {"source": "resume" if captured["options"].resume else "startup"},
                        None,
                        {},
                    )
                    text = output.get("hookSpecificOutput", {}).get("additionalContext")
                    if text:
                        captured["contexts"].append(text)
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, message):
            captured["message"] = message
            for matcher in (captured["options"].hooks or {}).get("UserPromptSubmit", []):
                for hook in matcher.hooks:
                    output = await hook({"prompt": message}, None, {})
                    text = output.get("hookSpecificOutput", {}).get("additionalContext")
                    if text:
                        captured["contexts"].append(text)

        async def interrupt(self):
            captured["results"].append("interrupted")

        async def get_context_usage(self):
            # 与本地 CN CLI 同形状：占用比例、压缩阈值与类别分解（只有百分比）。
            captured["context_queries"] = captured.get("context_queries", 0) + 1
            return {
                "contextWindow": {"usedPercentage": 42.0},
                "autoCompact": {"enabled": False, "thresholdPercentage": 83.5},
                "categories": [
                    {"type": "system_prompt", "percentage": 1.2},
                    {"type": "messages", "percentage": 40.8},
                ],
            }

        async def tool_call(self, item: ToolCall):
            url = captured["options"].mcp_servers[TOOL_SERVER_NAME]["url"]
            async with mcp_session(gateway.tool_server, url) as session:
                return await session.call_tool(item.name, item.fields)

        async def receive_response(self):
            for item in script:
                if isinstance(item, ToolCall):
                    captured["results"].append(await self.tool_call(item))
                else:
                    yield item

    monkeypatch.setattr(agent_client, "QoderSDKClient", StubClient)
    return captured


def result(error=False, text=None):
    return ResultMessage("success", 1, 1, error, 1, "session-1", result=text)


def test_stream_reuses_background_across_gateway_restart_and_appends_changes(settings, monkeypatch):
    gateway = make_gateway(settings)
    seed_memory(gateway.memory_store, "user", "常住南京")
    initial = install_sdk(monkeypatch, gateway, [result()])
    asyncio.run(collect(gateway.stream_turn(message_turn("你好"))))
    assert "常住南京" in "\n".join(initial["contexts"])

    restarted = make_gateway(settings)
    reused = install_sdk(monkeypatch, restarted, [result()])
    turn = Turn(
        kind=TurnKind.MESSAGE,
        task_id="task-1",
        sdk_session_id="session-1",
        message="继续",
        materials=(Material("本次事件", "新结果"),),
    )
    asyncio.run(collect(restarted.stream_turn(turn)))
    assert reused["contexts"] == ["## 本次事件\n新结果"]

    seed_memory(restarted.memory_store, "user", "常住苏州")
    changed = install_sdk(monkeypatch, restarted, [result()])
    asyncio.run(collect(restarted.stream_turn(replace(turn, materials=()))))
    text = "\n".join(changed["contexts"])
    assert "常住苏州" in text and "常住南京" not in text
    assert "完整替代此前版本" in text and FULL_NOTE not in text


def test_failed_stream_does_not_commit_material_checkpoint(settings, monkeypatch):
    gateway = make_gateway(settings)
    seed_memory(gateway.memory_store, "user", "重要背景")
    install_sdk(monkeypatch, gateway, [result(error=True, text="失败")])
    asyncio.run(collect(gateway.stream_turn(message_turn("你好"))))
    assert not (gateway.workspaces / "task-1" / ".context-materials.json").exists()


def test_content_filtered_after_partial_answer_is_a_structured_failure(settings, monkeypatch):
    gateway = make_gateway(settings)
    refused = replace(result(error=True), stop_reason="refusal")
    install_sdk(
        monkeypatch,
        gateway,
        [
            AssistantMessage([TextBlock("部分回答")], "qmodel_38max"),
            AssistantMessage(
                [TextBlock("This conversation contains sensitive content. Try switching models")],
                "<synthetic>",
                error="invalid_request",
            ),
            refused,
        ],
    )
    events = asyncio.run(collect(gateway.stream_turn(message_turn("你好"))))
    assert [event["text"] for event in events if event["type"] == "text"] == ["部分回答"]
    assert events[-1]["message"] == "模型服务因内容过滤停止了回答"
    assert events[-1]["failure"]["code"] == "model_content_filtered"
    assert events[-1]["failure"]["recovery"] == "new_session"


def test_sdk_exit_failure_does_not_commit_checkpoint_or_report_done(settings, monkeypatch):
    gateway = make_gateway(settings)
    seed_memory(gateway.memory_store, "user", "重要背景")
    install_sdk(monkeypatch, gateway, [result()])
    original = agent_client.QoderSDKClient

    class ExitFailure(original):
        async def __aexit__(self, *exc):
            raise RuntimeError("模拟会话保存失败")

    monkeypatch.setattr(agent_client, "QoderSDKClient", ExitFailure)
    with pytest.raises(FailureError):
        asyncio.run(collect(gateway.stream_turn(message_turn("你好"))))
    assert not (gateway.workspaces / "task-1" / ".context-materials.json").exists()


def test_sdk_exit_failure_cannot_replace_content_filter_reason(settings, monkeypatch):
    gateway = make_gateway(settings)
    install_sdk(
        monkeypatch,
        gateway,
        [
            AssistantMessage(
                [TextBlock("Session blocked, Please clear context try again")],
                "<synthetic>",
                error="invalid_request",
            ),
            replace(result(error=True), stop_reason="refusal"),
        ],
    )
    original = agent_client.QoderSDKClient

    class ExitFailure(original):
        async def __aexit__(self, *exc):
            raise RuntimeError("private-response")

    monkeypatch.setattr(agent_client, "QoderSDKClient", ExitFailure)
    events = asyncio.run(collect(gateway.stream_turn(message_turn("你好"))))
    assert events[-1]["failure"]["code"] == "model_content_filtered"
    assert "private-response" not in str(events)


def run_tools(gateway: QoderGateway, monkeypatch, *calls: ToolCall, task_id="task-1"):
    """在一个真实轮次里按序调用工具，返回事件与每次调用的结果。"""
    script = [SystemMessage("init", {"session_id": "session-1"}), *calls, result()]
    captured = install_sdk(monkeypatch, gateway, script)
    events = asyncio.run(collect(gateway.stream_turn(message_turn("测试输入", task_id=task_id))))
    return events, captured["results"]


def test_skill_review_uses_only_staged_skill_tools(settings, monkeypatch):
    init_db()
    skills = SkillService(settings.data_dir, settings.db_path)
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(),
            tasks=SessionStore(),
            gmail=MockGmailClient(),
            skills=skills,
        ),
        ToolServer(),
        settings=configured(settings),
    )
    task_id = gateway.tasks_store.create_task("复盘")["task_id"]
    captured = install_sdk(
        monkeypatch,
        gateway,
        [
            ToolCall(
                "skill_propose_change",
                {
                    "action": "create",
                    "payload": {
                        "skill_id": "learned",
                        "name": "方法",
                        "description": "可复用方法",
                        "body": "步骤",
                    },
                    "reason": "用户纠正后成功",
                    "evidence_refs_to_use": ["E1"],
                },
            ),
            ToolCall("gmail_send_message", {"to": ["someone@example.com"]}),
            result(),
        ],
    )
    candidates = asyncio.run(
        gateway.review_skills("review-1", task_id, "复盘指令", "轨迹材料", "auto", {"E1": "item-1"})
    )
    assert {name.rsplit("__", 1)[-1] for name in captured["options"].allowed_tools} == {
        "skill_list",
        "skill_view",
        "skill_propose_change",
    }
    assert len(candidates) == 1
    assert candidates[0]["evidence_item_ids"] == ["item-1"]
    assert skills.catalog() == []  # 会话只暂存候选；调度器在正常结束后应用。
    assert captured["results"][1].isError


def test_skill_review_rejects_bad_ref_then_accepts_corrected_ref(settings, monkeypatch):
    init_db()
    skills = SkillService(settings.data_dir, settings.db_path)
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(),
            tasks=SessionStore(),
            gmail=MockGmailClient(),
            skills=skills,
        ),
        ToolServer(),
        settings=configured(settings),
    )
    task_id = gateway.tasks_store.create_task("复盘")["task_id"]
    base = {
        "action": "create",
        "payload": {
            "skill_id": "learned",
            "name": "方法",
            "description": "可复用方法",
            "body": "步骤",
        },
        "reason": "用户纠正后成功",
    }
    captured = install_sdk(
        monkeypatch,
        gateway,
        [
            ToolCall("skill_propose_change", {**base, "evidence_refs_to_use": ["E99"]}),
            ToolCall("skill_propose_change", {**base, "evidence_refs_to_use": ["E1"]}),
            result(),
        ],
    )
    candidates = asyncio.run(
        gateway.review_skills(
            "review-1",
            task_id,
            "复盘指令",
            "[E1] user: 纠正",
            "auto",
            {"E1": "item-1"},
        )
    )
    assert captured["results"][0].isError
    assert not captured["results"][1].isError
    assert len(candidates) == 1
    assert candidates[0]["evidence_item_ids"] == ["item-1"]


def test_skill_review_bad_ref_without_retry_fails_window(settings, monkeypatch):
    init_db()
    skills = SkillService(settings.data_dir, settings.db_path)
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(),
            tasks=SessionStore(),
            gmail=MockGmailClient(),
            skills=skills,
        ),
        ToolServer(),
        settings=configured(settings),
    )
    task_id = gateway.tasks_store.create_task("复盘")["task_id"]
    install_sdk(
        monkeypatch,
        gateway,
        [
            ToolCall(
                "skill_propose_change",
                {
                    "action": "create",
                    "payload": {
                        "skill_id": "learned",
                        "name": "方法",
                        "description": "可复用方法",
                        "body": "步骤",
                    },
                    "reason": "用户纠正后成功",
                    "evidence_refs_to_use": ["E99"],
                },
            ),
            result(),
        ],
    )
    with pytest.raises(ExceptionGroup, match="unhandled errors in a TaskGroup") as error:
        asyncio.run(
            gateway.review_skills(
                "review-1",
                task_id,
                "复盘指令",
                "[E1] user: 纠正",
                "auto",
                {"E1": "item-1"},
            )
        )
    assert any(
        isinstance(item, AgentProtocolError) and "依据或用户可见理由无效，尚未纠正" in str(item)
        for item in error.value.exceptions
    )


ADD_CHINESE = {"action": "append", "target": "user", "text": "默认使用中文"}


def run_review(gateway, monkeypatch, *calls, task_id):
    install_sdk(monkeypatch, gateway, [*calls, result()])
    return asyncio.run(gateway.review_memory(task_id, "回顾指令", "回顾材料"))


def test_review_memory_records_real_tool_results(settings, monkeypatch):
    gateway = make_gateway(settings)
    task_id = gateway.tasks_store.create_task("回顾记忆")["task_id"]

    # 即使误混入前台与另一个后台会话的工具，也只注册回顾会话的工具。
    gateway.review_tools.extend(gateway.tools)
    records = run_review(
        gateway,
        monkeypatch,
        ToolCall("memory_edit", {"operations": [ADD_CHINESE]}),
        task_id=task_id,
    )

    assert [record["tool"] for record in records] == ["memory_edit"]
    first = records[0]
    assert first["arguments"] == {"operations": [ADD_CHINESE], "task_id": task_id}
    assert first["result"]["planned"] is True
    assert first["result"]["operations"] == [ADD_CHINESE]
    assert (settings.data_dir / "memory" / "USER.md").read_text() == ""


def test_review_memory_records_structured_errors(settings, monkeypatch):
    gateway = make_gateway(settings)
    task_id = gateway.tasks_store.create_task("回顾记忆")["task_id"]

    records = run_review(
        gateway,
        monkeypatch,
        ToolCall(
            "memory_edit",
            {"operations": [{"action": "append", "target": "user", "text": "甲" * 1376}]},
        ),
        task_id=task_id,
    )

    error = records[0]["error"]
    assert {key: error[key] for key in ("error", "message", "target", "used", "limit")} == {
        "error": "memory_full",
        "message": "“关于你”放不下：保存后需要 1376 个字符，上限为 1375",
        "target": "user",
        "used": 1376,
        "limit": 1375,
    }
    assert error["memory"]["user"]["content"] == "（空）"
    assert gateway.memory_store.snapshot()["user"]["content"] == ""


def test_draft_saved_precedes_following_text(settings, monkeypatch):
    tasks = SessionStore()
    drafts = MailDraftStore()
    init_db()
    task_id = tasks.create_task("处理新收到的邮件")["task_id"]
    gateway = QoderGateway(
        ToolDeps(drafts=drafts, tasks=tasks, gmail=MockGmailClient()),
        ToolServer(),
        settings=configured(settings),
    )
    captured = install_sdk(
        monkeypatch,
        gateway,
        [
            SystemMessage("init", {"session_id": "session-1"}),
            ToolCall("gmail_prepare_reply", dict(DRAFT)),
            StreamEvent(
                "e",
                "session-1",
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "草稿已"}},
            ),
            StreamEvent(
                "e",
                "session-1",
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "保存"}},
            ),
            AssistantMessage([TextBlock("草稿已保存")], "model"),
            result(),
        ],
    )

    events = asyncio.run(
        collect(gateway.stream_turn(message_turn("帮我写一封回信", task_id=task_id)))
    )

    assert [event["type"] for event in events] == ["session", "draft_saved", "text", "text", "done"]
    saved = events[1]
    assert saved["version"] == 1
    assert drafts.get_draft(saved["operation_id"])["body"] == DRAFT["body"]
    # 增量输出已经送过，整段文本不再重复；调用参数与用户消息原样传给模型。
    assert "".join(event.get("text", "") for event in events) == "草稿已保存"
    assert captured["message"] == "帮我写一封回信"
    assert captured["options"].resume is None


def test_later_message_without_deltas_still_sends_full_text(settings, monkeypatch):
    """去重只按消息生效：前一条有增量、后一条没有时，后一条的整段文本不能丢。"""
    gateway = make_gateway(settings)
    install_sdk(
        monkeypatch,
        gateway,
        [
            SystemMessage("init", {"session_id": "session-1"}),
            StreamEvent(
                "e",
                "session-1",
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "先看"}},
            ),
            AssistantMessage([TextBlock("先看")], "model"),
            ToolCall("gmail_get_message", {"message_id": "msg_invite_001"}),
            AssistantMessage([TextBlock("结论如下")], "model"),
            result(),
        ],
    )

    events = asyncio.run(collect(gateway.stream_turn(message_turn("读邮件"))))

    assert [event["type"] for event in events] == ["session", "text", "text", "done"]
    assert "".join(event["text"] for event in events if event["type"] == "text") == "先看结论如下"


def test_tool_calls_announce_the_current_step_before_they_run(settings, monkeypatch):
    """模型给出工具调用时先告诉页面这一步在做什么；没有声明说明、本轮不可见的工具不展示。"""
    gateway = make_gateway(settings)
    captured = install_sdk(
        monkeypatch,
        gateway,
        [
            SystemMessage("init", {"session_id": "session-1"}),
            AssistantMessage(
                [
                    TextBlock("我先查一下。"),
                    ToolUseBlock("t1", "mcp__pebble__gmail_search", {"query": "活动邀请"}),
                ],
                "model",
            ),
            ToolCall("gmail_search", {"query": "活动邀请"}),
            AssistantMessage(
                [ToolUseBlock("t2", "WebSearch", {"query": "第二会议室 位置"})], "model"
            ),
            # 新邮件轮之外才可见的工具、未知工具与内置的其他工具都不产生步骤
            AssistantMessage(
                [
                    ToolUseBlock("t3", "mcp__pebble__not_registered", {}),
                    ToolUseBlock("t4", "Bash", {"command": "ls"}),
                ],
                "model",
            ),
            AssistantMessage([TextBlock("找到了。")], "model"),
            result(),
        ],
    )

    events = asyncio.run(collect(gateway.stream_turn(message_turn("找一下邀请邮件"))))

    assert [(event["type"], event.get("text")) for event in events] == [
        ("session", None),
        ("text", "我先查一下。"),
        ("activity", "正在搜索邮件：活动邀请"),
        ("activity", "正在联网搜索：第二会议室 位置"),
        ("text", "找到了。"),
        ("done", None),
    ]
    assert captured["results"][0].isError is False


def test_step_description_failure_is_skipped(settings, monkeypatch):
    gateway = make_gateway(settings)
    search = next(tool for tool in gateway.tools if tool.name == "gmail_search")

    def broken(_arguments):
        raise RuntimeError("模拟说明生成失败")

    gateway.tools = [
        replace(tool, activity_renderer=broken) if tool is search else tool
        for tool in gateway.tools
    ]
    install_sdk(
        monkeypatch,
        gateway,
        [
            AssistantMessage(
                [ToolUseBlock("t1", "mcp__pebble__gmail_search", {"query": "x"})], "model"
            ),
            result(),
        ],
    )

    events = asyncio.run(collect(gateway.stream_turn(message_turn("搜邮件"))))

    assert [event["type"] for event in events] == ["done"]


def test_repeated_init_announces_session_once(settings, monkeypatch):
    """CLI 一轮会上报多次会话建立：同一标识只广播一次，变化时照常上报。"""
    gateway = make_gateway(settings)
    install_sdk(
        monkeypatch,
        gateway,
        [
            SystemMessage("init", {"session_id": "session-1"}),
            SystemMessage("init", {"session_id": "session-1"}),
            SystemMessage("init", {"session_id": "session-2"}),
            result(),
        ],
    )

    events = asyncio.run(collect(gateway.stream_turn(message_turn("回复"))))

    assert events == [
        {"type": "session", "sdk_session_id": "session-1"},
        {"type": "session", "sdk_session_id": "session-2"},
        {"type": "done"},
    ]


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ([SystemMessage("init", {"session_id": "s"})], agent_client.NO_TERMINAL_MESSAGE),
        (
            [SystemMessage("init", {"session_id": "s"}), result(error=True, text="额度不足")],
            agent_client.MODEL_ERROR_MESSAGE,
        ),
        (
            [SystemMessage("init", {"session_id": "s"}), result(error=True)],
            agent_client.MODEL_ERROR_MESSAGE,
        ),
    ],
)
def test_abnormal_end_becomes_error_event(settings, monkeypatch, script, reason):
    gateway = make_gateway(settings)
    install_sdk(monkeypatch, gateway, script)

    events = asyncio.run(collect(gateway.stream_turn(message_turn("回复"))))

    assert [event["type"] for event in events] == ["session", "error"]
    assert events[-1]["message"] == reason


def test_interrupt_turn_reaches_the_active_client(settings, monkeypatch):
    """终止请求送到本轮仍在会话中的客户端；轮次结束句柄摘除，再请求返回 False。"""
    gateway = make_gateway(settings)
    interrupted = asyncio.Event()
    calls: list[str] = []

    class InterruptibleClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, message):
            pass

        async def interrupt(self):
            calls.append("interrupt")
            interrupted.set()

        async def receive_response(self):
            yield SystemMessage("init", {"session_id": "session-1"})
            await interrupted.wait()
            yield result()

    monkeypatch.setattr(agent_client, "QoderSDKClient", InterruptibleClient)

    async def scenario():
        turn = Turn(
            kind=TurnKind.MESSAGE,
            task_id="task-1",
            sdk_session_id=None,
            message="测试输入",
            run_id="run-1",
        )
        events = []

        async def consume():
            async for event in gateway.stream_turn(turn):
                events.append(event)

        consumer = asyncio.create_task(consume())
        async with asyncio.timeout(5):
            while not gateway._interrupts:
                await asyncio.sleep(0.01)
            assert await gateway.interrupt_turn("run-1") is True
            await consumer
        return events

    events = asyncio.run(scenario())

    assert calls == ["interrupt"]
    assert gateway._interrupts == {}
    assert asyncio.run(gateway.interrupt_turn("run-1")) is False
    assert [event["type"] for event in events] == ["session", "done"]


# ---------- 工具边界 ----------


def test_tool_boundary_returns_structured_business_errors(settings, monkeypatch):
    tasks = SessionStore()
    init_db()
    task_id = tasks.create_task("处理新收到的邮件")["task_id"]
    other_task = tasks.create_task("另一个任务")["task_id"]
    gateway = make_gateway(settings)

    events, (invalid, saved) = run_tools(
        gateway,
        monkeypatch,
        ToolCall("gmail_prepare_reply", {**DRAFT, "to": ["not-an-address"], "subject": "  "}),
        ToolCall("gmail_prepare_reply", dict(DRAFT)),
        task_id=task_id,
    )
    assert invalid.isError is True
    payload = tool_payload(invalid)
    assert payload["error"] == "invalid_draft"
    assert {item["field"] for item in payload["errors"]} == {"to", "subject"}

    assert saved.isError is False
    created = tool_payload(saved)
    assert created["version"] == 1
    assert [event["type"] for event in events if event["type"] == "draft_saved"] == ["draft_saved"]
    assert [op["operation_id"] for op in tasks.list_task_operations(task_id)] == [
        created["operation_id"]
    ]

    _, (conflict,) = run_tools(
        gateway,
        monkeypatch,
        ToolCall(
            "gmail_update_draft",
            {
                "operation_id": created["operation_id"],
                "expected_version": 7,
                "to": DRAFT["to"],
                "subject": DRAFT["subject"],
                "body": "改写正文",
            },
        ),
        task_id=task_id,
    )
    assert conflict.isError is True
    conflict_payload = tool_payload(conflict)
    assert conflict_payload.pop("failure")["code"] == "version_conflict"
    assert conflict_payload == {
        "error": "version_conflict",
        "message": "当前版本为 1",
        "current_version": 1,
    }

    # 另一个任务读同一操作：既不成功，也不透露它存在于别处。
    _, (hidden,) = run_tools(
        gateway,
        monkeypatch,
        ToolCall("gmail_read_draft", {"operation_id": created["operation_id"]}),
        task_id=other_task,
    )
    assert hidden.isError is True
    assert tool_payload(hidden)["error"] == "not_found"


def test_tool_boundary_hides_unexpected_failure_detail(settings, monkeypatch):
    class Exploding(MockGmailClient):
        def get_message(self, message_id):
            raise RuntimeError("secret must not leak")

    gateway = make_gateway(settings, gmail=Exploding())

    _, (failed,) = run_tools(
        gateway, monkeypatch, ToolCall("gmail_get_message", {"message_id": "msg_invite_001"})
    )

    assert failed.isError is True
    payload = tool_payload(failed)
    assert payload["error"] == "unexpected"
    assert "secret" not in json.dumps(payload, ensure_ascii=False)


def test_foreground_memory_tool_reads_and_writes_through_mcp(settings, monkeypatch):
    from tests.support import memory_anchor

    gateway = make_gateway(settings)
    seed_memory(gateway.memory_store, "user", "旧偏好")
    anchor = memory_anchor(gateway.memory_store, "旧偏好")
    _, results = run_tools(
        gateway,
        monkeypatch,
        ToolCall("memory_edit", {}),
        ToolCall(
            "memory_edit",
            {"operations": [{"action": "replace", "anchor": anchor, "text": "新偏好"}]},
        ),
    )
    assert len(results) == 2
    assert gateway.memory_store.snapshot()["user"]["content"] == "新偏好"
