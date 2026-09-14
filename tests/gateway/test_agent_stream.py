"""SDK 网关：选项装配、事件映射、工具边界与会话历史读取。

只替换 SDK 客户端本身：工具经进程内 MCP 端点按真实协议调用，存储与草稿都是真实实现。
"""

import asyncio
import json
from dataclasses import dataclass

import pytest
from qodercn_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    project_key_for_directory,
)

from server.agent import client as agent_client
from server.agent.client import QoderGateway
from server.agent.context import Material, assemble
from server.agent.mcp import MCP_MOUNT_PATH, TOOL_SERVER_NAME, ToolServer
from server.agent.prompt import BASE_PROMPT
from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, exposed_tools
from server.config import Settings
from server.db import init_db
from server.errors import DependencyUnavailableError
from server.gateway.agent_contract import Turn
from server.gateway.runtime import execution_result_content
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.gmail.trigger import new_mail_content
from server.tools.registry import SideEffect, ToolDefinition
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
    # 内置工具与本机设置关闭：模型只能连本轮登记的 MCP 端点。
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
    assert options.cwd == gateway.workspace


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

    assert '"status": "sent"' in options.system_prompt
    assert "op1" in options.system_prompt
    # 回传材料只进系统提示，不伪装成用户说过的话。
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

    # 标识以 JSON 材料块进系统提示，模型不必从散文句子里解析。
    assert '"source_message_id": "msg-1"' in options.system_prompt
    assert '"thread_id": "th-1"' in options.system_prompt
    assert "msg-1" not in turn.message


def test_materials_render_by_content_type():
    ctx = assemble(materials=[Material("备忘", "周二下午有组会"), Material("载荷", {"a": 1})])

    # 基础提示固定在最前，材料按序追加；自由文本按原文渲染，结构化数据渲染为 JSON 块。
    assert ctx.system_prompt.startswith(BASE_PROMPT)
    assert "## 备忘\n周二下午有组会" in ctx.system_prompt
    assert '## 载荷\n{\n  "a": 1\n}' in ctx.system_prompt


def test_managed_model_and_byok_credentials(settings):
    managed = options_for(make_gateway(settings, qoder_model="q-model"), TurnKind.MESSAGE)
    assert managed.model == "q-model"
    assert managed.resolve_model is None

    byok = make_gateway(
        settings,
        qoder_model="gpt-x",
        model_provider="openai",
        model_api_key="sk-test",
        model_base_url="https://models.example.com",
    )
    options = options_for(byok, TurnKind.MESSAGE)
    assert options.model is None
    assert options.resolve_model(None) == {
        "model": {
            "provider": "openai",
            "model": "gpt-x",
            "api_key": "sk-test",
            "style": "openai",
            "url": "https://models.example.com",
        }
    }
    assert "sk-test" not in options.system_prompt


def test_partial_byok_config_fails_at_assembly(settings):
    """BYOK 只配一部分不能静默退回托管模型：装配期就报错并指出缺项。"""
    with pytest.raises(DependencyUnavailableError, match="PEBBLE_QODER_MODEL"):
        make_gateway(settings, model_provider="openai", model_api_key="sk-test")

    with pytest.raises(DependencyUnavailableError, match="PEBBLE_MODEL_API_KEY"):
        make_gateway(settings, model_provider="openai", qoder_model="gpt-x")


def test_unregistered_provider_fails_at_assembly(settings):
    with pytest.raises(DependencyUnavailableError, match="未登记的模型供应商"):
        make_gateway(
            settings,
            model_provider="not-a-provider",
            model_api_key="sk-test",
            qoder_model="gpt-x",
        )


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


# ---------- 会话历史 ----------


def test_history_reads_persisted_transcript(settings):
    gateway = make_gateway(settings)
    project = (
        settings.data_dir
        / "agent"
        / "config"
        / "projects"
        / project_key_for_directory(gateway.workspace)
    )
    project.mkdir(parents=True)
    session_id, user_id, assistant_id = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    )
    entries = [
        {
            "uuid": user_id,
            "parentUuid": None,
            "sessionId": session_id,
            "type": "user",
            "message": {"role": "user", "content": "帮我写一封回信"},
        },
        {
            "uuid": assistant_id,
            "parentUuid": user_id,
            "sessionId": session_id,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "不展示"},
                    {"type": "tool_use", "id": "t1", "name": "gmail_read_draft", "input": {}},
                    {"type": "text", "text": "草稿已准备好"},
                ],
            },
        },
    ]
    (project / f"{session_id}.jsonl").write_text("\n".join(json.dumps(e) for e in entries))

    history = asyncio.run(gateway.read_history(task_id="task-1", sdk_session_id=session_id))

    assert history == [
        {"role": "user", "text": "帮我写一封回信"},
        {"role": "assistant", "text": "草稿已准备好"},
    ]
    assert asyncio.run(gateway.read_history(task_id="task-1", sdk_session_id=None)) == []
