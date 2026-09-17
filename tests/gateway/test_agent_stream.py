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
from server.agent.mcp import MCP_MOUNT_PATH, TOOL_SERVER_NAME, ToolServer
from server.agent.prompt import BASE_PROMPT
from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, exposed_tools
from server.config import Settings
from server.db import init_db
from server.errors import DependencyUnavailableError
from server.gateway.agent_contract import Turn, TurnAttachment
from server.gateway.runtime import execution_result_content
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.gmail.trigger import new_mail_content
from server.tools.personal_kb.service import KbStore
from server.tools.registry import SideEffect, ToolDefinition
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
    visible = exposed_tools(gateway.tools, allowed=ALLOWED_EFFECTS[turn.kind])
    return gateway._options(turn, visible=visible, path=f"{MCP_MOUNT_PATH}/{TURN_TOKEN}")


def injected_context(options) -> str:
    matcher = options.hooks["SessionStart"][0]
    result = asyncio.run(matcher.hooks[0]({}, None, {}))
    return result["hookSpecificOutput"]["additionalContext"]


def message_turn(message: str, *, task_id: str = "task-1") -> Turn:
    return Turn(kind=TurnKind.MESSAGE, task_id=task_id, sdk_session_id=None, message=message)


async def collect(stream):
    return [event async for event in stream]


# ---------- 选项装配 ----------


def test_new_mail_turn_sees_only_readonly_tools(settings):
    gateway = make_gateway(settings)
    options = options_for(gateway, TurnKind.NEW_MAIL)

    readonly = exposed_tools(gateway.tools, allowed=ALLOWED_EFFECTS[TurnKind.NEW_MAIL])
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
            "url": f"http://127.0.0.1:{gateway.settings.port}{MCP_MOUNT_PATH}/{TURN_TOKEN}",
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
    unexpected = asyncio.run(options.can_use_tool("Bash", {"command": "true"}, context))

    assert isinstance(fetch, PermissionResultAllow)
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
    outside = asyncio.run(
        options.can_use_tool("Read", {"file_path": "../task-2/attachments/file-2"}, context)
    )
    shell = asyncio.run(options.can_use_tool("Bash", {"command": "true"}, context))
    assert isinstance(inside, PermissionResultAllow)
    assert isinstance(outside, PermissionResultDeny)
    assert isinstance(shell, PermissionResultDeny)


def test_image_attachment_uses_structured_base64_input(settings):
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

    payload = asyncio.run(collect(gateway._query_input(turn)))[0]
    content = payload["message"]["content"]
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image"
    assert content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "cmFuZG9tLWltYWdl",
    }


@pytest.mark.parametrize("kind", list(TurnKind))
def test_memory_tools_are_never_visible_in_foreground_turns(settings, kind):
    names = {
        name.rsplit("__", 1)[-1] for name in options_for(make_gateway(settings), kind).allowed_tools
    }

    # 记忆写入只发生在判断与回顾的一次性会话，前台任何轮次都看不到记忆工具。
    assert not {name for name in names if name.startswith("memory")}


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
            "url": f"http://127.0.0.1:{gateway.settings.port}{MCP_MOUNT_PATH}/{TURN_TOKEN}",
        }
    }
    assert options.allowed_mcp_server_names == [TOOL_SERVER_NAME]
    assert options.system_prompt == "回顾指令"
    assert options.resume is None
    assert options.hooks is None
    assert options.include_partial_messages is False


def test_judge_options_expose_judgment_tools_in_fresh_session(settings):
    gateway = make_gateway(settings)
    options = gateway._oneshot_options(
        "判断指令",
        f"{MCP_MOUNT_PATH}/{TURN_TOKEN}",
        gateway.judge_tools,
        task_id="task-1",
        model="q-model",
    )

    assert options.allowed_tools == [
        f"mcp__{TOOL_SERVER_NAME}__{name}" for name in ("memory_edit", "memory_ask")
    ]
    assert options.tools == []
    assert options.system_prompt == "判断指令"
    assert options.resume is None
    assert options.hooks is None
    assert options.include_partial_messages is False


@pytest.mark.parametrize("kind", list(TurnKind))
def test_external_write_tools_are_never_visible_to_the_model(settings, kind):
    gateway = make_gateway(settings)
    send_now = ToolDefinition(
        name="gmail_send_message",
        description="真实发送邮件；只由 Confirmation 调用。",
        func=lambda **_: None,
        side_effect=SideEffect.EXTERNAL_WRITE,
    )
    gateway.tools = [*gateway.tools, send_now]

    options = options_for(gateway, kind)

    assert exposed_tools([send_now], allowed=ALLOWED_EFFECTS[kind]) == []
    assert not any("send" in name for name in options.allowed_tools)


def test_execution_result_enters_system_prompt(settings):
    gateway = make_gateway(settings)
    message, materials = execution_result_content(
        operation_id="op1", version=2, result={"status": "sent"}
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
    assert '"status": "sent"' in injected_context(options)
    assert "op1" in injected_context(options)
    # 回传材料只进附加上下文，不伪装成用户说过的话。
    assert "op1" not in turn.message


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
    assert '"source_message_id": "msg-1"' in injected_context(options)
    assert '"thread_id": "th-1"' in injected_context(options)
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
    assert injected_context(first) == "## 当前生效规则\n回答先给结论"
    assert injected_context(second) == "## 当前生效规则\n回答先解释推导"


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

    assert injected_context(first).startswith("## 关于你\n回答先给结论")
    assert injected_context(second) == (
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
    assert injected_context(options_for(gateway, TurnKind.MESSAGE)) == "## 关于你\n回答先给结论"

    kb.save(title="星云验收纪要", body="通过。", path="项目/验收", summary="二期验收结论")
    options = options_for(gateway, TurnKind.NEW_MAIL, materials=(Material("本轮材料", "新邮件"),))

    assert injected_context(options) == (
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
    assert injected_context(options) == "## 本轮材料\n照常进行"


def test_turn_without_materials_does_not_register_context_hook(settings):
    options = options_for(make_gateway(settings), TurnKind.MESSAGE)

    assert options.hooks is None


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


def test_managed_model_and_byok_credentials(settings):
    managed = options_for(make_gateway(settings, qoder_model="q-model"), TurnKind.MESSAGE)
    assert managed.model == "q-model"
    assert managed.resolve_model is None

    byok = make_gateway(
        settings,
        qoder_model="deepseek-v4-pro-pg",
        model_provider="deepseek",
        model_api_key="sk-test",
        model_base_url="https://api.deepseek.com",
    )
    options = options_for(byok, TurnKind.MESSAGE)
    assert options.model is None
    assert options.resolve_model(None) == {
        "model": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro-pg",
            "api_key": "sk-test",
            "style": "openai",
            "url": "https://api.deepseek.com",
        }
    }
    assert "sk-test" not in options.system_prompt


def test_title_call_uses_its_own_managed_model(settings):
    """标题是每条任务一次的后台短调用，可以走低倍率型号；主对话不受影响。"""
    gateway = make_gateway(settings, qoder_model="q-model", title_model="q-cheap")
    assert gateway._title_options().model == "q-cheap"
    assert options_for(gateway, TurnKind.MESSAGE).model == "q-model"

    inherited = make_gateway(settings, qoder_model="q-model")
    assert inherited._title_options().model == "q-model"


def test_byok_title_call_keeps_the_session_model(settings):
    """BYOK 的密钥与供应商标识绑定，标题调用不能借用托管型号名换模型。"""
    byok = make_gateway(
        settings,
        qoder_model="deepseek-v4-pro-pg",
        title_model="q-cheap",
        model_provider="deepseek",
        model_api_key="sk-test",
    )
    options = byok._title_options()
    assert options.model is None
    assert options.resolve_model(None) == {
        "model": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro-pg",
            "api_key": "sk-test",
            "style": "openai",
        }
    }


def test_partial_byok_config_fails_at_assembly(settings):
    """BYOK 只配一部分不能静默退回托管模型：装配期就报错并指出缺项。"""
    with pytest.raises(DependencyUnavailableError, match="PEBBLE_QODER_MODEL"):
        make_gateway(settings, model_provider="deepseek", model_api_key="sk-test")

    with pytest.raises(DependencyUnavailableError, match="PEBBLE_MODEL_API_KEY"):
        make_gateway(settings, model_provider="deepseek", qoder_model="deepseek-v4-pro-pg")


def test_unregistered_provider_fails_at_assembly(settings):
    """登记表以 `list_byok_providers()` 目录为准，报错时要能看出该填什么。"""
    with pytest.raises(DependencyUnavailableError, match="未登记的模型供应商：qwen") as error:
        make_gateway(
            settings,
            model_provider="qwen",
            model_api_key="sk-test",
            qoder_model="qwen3.8-max-tp",
        )
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
    captured = {"options": None, "message": None, "results": []}

    class StubClient:
        def __init__(self, options):
            captured["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, message):
            captured["message"] = message

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


def run_tools(gateway: QoderGateway, monkeypatch, *calls: ToolCall, task_id="task-1"):
    """在一个真实轮次里按序调用工具，返回事件与每次调用的结果。"""
    script = [SystemMessage("init", {"session_id": "session-1"}), *calls, result()]
    captured = install_sdk(monkeypatch, gateway, script)
    events = asyncio.run(collect(gateway.stream_turn(message_turn("测试输入", task_id=task_id))))
    return events, captured["results"]


def run_judge(gateway: QoderGateway, monkeypatch, *calls: ToolCall, task_id="task-1"):
    """在一次性判断会话里按序调用工具，返回记录到的工具调用与结果。"""
    script = [*calls, result()]
    install_sdk(monkeypatch, gateway, script)
    return asyncio.run(gateway.judge_memory(task_id, "判断指令", "判断材料"))


ADD_CHINESE = {"action": "append", "target": "user", "text": "默认使用中文"}


def test_judge_memory_records_real_tool_results(settings, monkeypatch):
    gateway = make_gateway(settings)
    task_id = gateway.tasks_store.create_task("判断记忆")["task_id"]

    records = run_judge(
        gateway,
        monkeypatch,
        ToolCall("memory_edit", {"operations": [ADD_CHINESE]}),
        ToolCall("memory_ask", {"question": "要改哪一条？"}),
        task_id=task_id,
    )

    assert [record["tool"] for record in records] == ["memory_edit", "memory_ask"]
    first = records[0]
    assert first["arguments"] == {"operations": [ADD_CHINESE]}
    assert first["result"]["changed"] is True
    assert first["result"]["applied"][0]["added"] == ["默认使用中文"]
    assert records[1]["result"] == {"question": "要改哪一条？"}
    assert (settings.data_dir / "memory" / "USER.md").read_text() == "默认使用中文"


def test_judge_memory_records_structured_errors(settings, monkeypatch):
    gateway = make_gateway(settings)
    task_id = gateway.tasks_store.create_task("判断记忆")["task_id"]

    records = run_judge(
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
            "额度不足",
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
    assert tool_payload(conflict) == {
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


def test_schema_hides_injected_dependencies(settings):
    gateway = make_gateway(settings)
    for definition in gateway.tools:
        assert not {"gmail", "drafts", "tasks", "task_id"} & set(
            definition.parameters_schema["properties"]
        )
