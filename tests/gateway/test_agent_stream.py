"""SDK 事件流与工具边界：会话事件顺序、结束事件，以及交回模型的工具结果形状。"""

import asyncio
import json

import pytest
from qodercn_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock

from server.agent import sdk_client
from server.agent.toolset import build_tools
from server.config import Settings
from server.db import init_db
from server.sessions.service import SessionStore
from server.tools.gmail.client import MockGmailClient
from server.tools.gmail.service import ReplyDraftStore
from server.tools.gmail.tools import prepare_reply
from server.tools.gmail.validator import validate_reply_draft


async def collect(stream):
    return [event async for event in stream]


def gateway() -> sdk_client.QoderGateway:
    """装配一套只用于事件流断言的网关；本组测试不调用工具，依赖不接触数据库。"""
    return sdk_client.QoderGateway(build_tools(drafts=ReplyDraftStore(None), tasks=SessionStore()))


def install_sdk_stub(monkeypatch, messages):
    captured = []

    class Client:
        def __init__(self, options):
            captured.append(options)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, message):
            self.message = message

        async def receive_response(self):
            for message in messages:
                yield message

    monkeypatch.setattr(sdk_client, "QoderSDKClient", Client)
    return captured


def result(error=False):
    return ResultMessage("success", 1, 1, error, 1, "session1")


def test_stream_resume_delta_and_no_duplicate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=tmp_path, QODERCN_PERSONAL_ACCESS_TOKEN="test-token", _env_file=None
        ),
    )
    captured = install_sdk_stub(
        monkeypatch,
        [
            SystemMessage("init", {"session_id": "session1"}),
            StreamEvent(
                "e",
                "session1",
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "草稿"}},
            ),
            AssistantMessage([TextBlock("草稿")], "model"),
            result(),
        ],
    )
    events = asyncio.run(
        collect(gateway().stream_message(task_id="task", sdk_session_id="session1", message="回复"))
    )
    assert [event["type"] for event in events] == ["session", "text", "done"]
    assert captured[0].resume == "session1"
    assert captured[0].tools == []
    assert not any("send_reply" in tool for tool in captured[0].allowed_tools)
    assert captured[0].setting_sources == []


@pytest.mark.parametrize("ending", [[], [result(True)]])
def test_stream_abnormal_end(monkeypatch, tmp_path, ending):
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=tmp_path, QODERCN_PERSONAL_ACCESS_TOKEN="test-token", _env_file=None
        ),
    )
    install_sdk_stub(monkeypatch, [SystemMessage("init", {"session_id": "session1"}), *ending])
    events = asyncio.run(
        collect(gateway().stream_message(task_id="task", sdk_session_id=None, message="回复"))
    )
    assert [event["type"] for event in events] == ["session", "error"]


@pytest.mark.parametrize("status", ["sent", "failed", "unknown"])
def test_result_returns_to_original_session(monkeypatch, tmp_path, status):
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=tmp_path, QODERCN_PERSONAL_ACCESS_TOKEN="test-token", _env_file=None
        ),
    )
    captured = install_sdk_stub(
        monkeypatch, [SystemMessage("init", {"session_id": "session1"}), result()]
    )
    events = asyncio.run(
        collect(
            gateway().stream_execution_result(
                task_id="task",
                sdk_session_id="session1",
                operation_id="op1",
                version=2,
                result={"status": status, "message_id": "sent1"},
            )
        )
    )
    assert captured[0].resume == "session1"
    assert f'"status": "{status}"' in captured[0].system_prompt
    assert events[-1] == {"type": "done"}


def test_schema_hides_injected_dependencies_and_types_recipients():
    schema = prepare_reply.parameters_schema
    assert "drafts" not in schema["properties"]
    assert schema["properties"]["to"] == {"type": "array", "items": {"type": "string"}}
    for definition in build_tools(drafts=ReplyDraftStore(None), tasks=SessionStore()):
        assert not {"client", "drafts", "tasks"} & set(definition.parameters_schema["properties"])


def capture_tool_handlers(monkeypatch, tools, task_id):
    """取出注册给 SDK 的 MCP 处理函数，用于断言交回模型的结果形状。"""
    handlers = {}
    original = sdk_client.tool

    def register(name, description, schema):
        def decorate(handler):
            handlers[name] = handler
            return original(name, description, schema)(handler)

        return decorate

    monkeypatch.setattr(sdk_client, "tool", register)
    sdk_client.build_options(tools, task_id)
    monkeypatch.setattr(sdk_client, "tool", original)

    async def call(name, fields):
        response = await handlers[name](fields)
        return response["isError"], json.loads(response["content"][0]["text"])

    return call


def test_tool_boundary_returns_structured_business_errors(settings, monkeypatch):
    """已知业务失败带结构化原因交回模型；不属于本任务的操作按对象不存在处理。"""
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=settings.data_dir, QODERCN_PERSONAL_ACCESS_TOKEN="test", _env_file=None
        ),
    )
    init_db()
    tasks = SessionStore()
    drafts = ReplyDraftStore(validate_reply_draft)
    tools = build_tools(drafts=drafts, tasks=tasks, gmail=MockGmailClient())
    task_id = tasks.create_task("处理新收到的邮件")["task_id"]
    other_task = tasks.create_task("另一个任务")["task_id"]
    call = capture_tool_handlers(monkeypatch, tools, task_id)
    call_as_other = capture_tool_handlers(monkeypatch, tools, other_task)

    draft = {
        "source_message_id": "msg_invite_001",
        "thread_id": "thread_invite_001",
        "to": ["alice@example.com"],
        "subject": "Re: 邀请",
        "body": "正文",
    }

    failed, payload = asyncio.run(
        call("gmail_prepare_reply", {**draft, "to": ["not-an-address"], "subject": "  "})
    )
    assert failed is True
    assert payload["error"] == "invalid_draft"
    assert {item["field"] for item in payload["errors"]} == {"to", "subject"}

    failed, saved = asyncio.run(call("gmail_prepare_reply", draft))
    assert failed is False
    assert saved["version"] == 1

    failed, payload = asyncio.run(
        call(
            "gmail_update_reply_draft",
            {
                "operation_id": saved["operation_id"],
                "expected_version": 7,
                "to": draft["to"],
                "subject": draft["subject"],
                "body": "改写正文",
            },
        )
    )
    assert failed is True
    assert payload == {
        "error": "version_conflict",
        "message": "当前版本为 1",
        "current_version": 1,
    }

    failed, payload = asyncio.run(
        call("gmail_read_reply_draft", {"operation_id": saved["operation_id"]})
    )
    assert (failed, payload["version"]) == (False, 1)

    # 另一个任务读同一操作：既不成功，也不透露它存在于别处
    failed, payload = asyncio.run(
        call_as_other("gmail_read_reply_draft", {"operation_id": saved["operation_id"]})
    )
    assert failed is True
    assert payload["error"] == "not_found"


def test_tool_boundary_hides_unexpected_failure_detail(settings, monkeypatch):
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=settings.data_dir, QODERCN_PERSONAL_ACCESS_TOKEN="test", _env_file=None
        ),
    )

    class Exploding(MockGmailClient):
        def get_message(self, message_id):
            raise RuntimeError("secret must not leak")

    tools = build_tools(drafts=ReplyDraftStore(None), tasks=SessionStore(), gmail=Exploding())
    call = capture_tool_handlers(monkeypatch, tools, "task")
    failed, payload = asyncio.run(call("gmail_get_message", {"message_id": "msg_invite_001"}))
    assert failed is True
    assert payload["error"] == "unexpected"
    assert "secret" not in json.dumps(payload, ensure_ascii=False)
