"""A/B 联合验证：真实业务模块、SQLite、HTTP；只替换 SDK 和 Gmail 的外部边界。"""

import asyncio
import json
import time
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from qodercn_agent_sdk import ResultMessage, SystemMessage

from server.agent import sdk_client
from server.agent.toolset import build_tools
from server.config import Settings
from server.db import init_db
from server.main import create_app
from server.sessions.service import SessionStore
from server.tools.gmail.sender import send_reply
from server.tools.gmail.service import ReplyDraftStore
from server.tools.gmail.sync import GmailSource
from server.tools.registry import SideEffect, ToolRegistry
from tests.support.gmail_double import MockGmailClient


def test_sdk_tools_to_http_confirmation(settings, monkeypatch):
    init_db()
    tasks = SessionStore()
    drafts = ReplyDraftStore()
    gmail = MockGmailClient()
    tools = build_tools(drafts=drafts, tasks=tasks, gmail=gmail)
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=settings.data_dir, QODERCN_PERSONAL_ACCESS_TOKEN="test", _env_file=None
        ),
    )
    handlers = {}
    original_tool = sdk_client.tool

    def register(name, description, schema):
        assert "task_id" not in schema["properties"]

        def decorate(handler):
            handlers[name] = handler
            return original_tool(name, description, schema)(handler)

        return decorate

    monkeypatch.setattr(sdk_client, "tool", register)
    sessions = []
    turns = []
    operation = {}

    class SDK:
        def __init__(self, options):
            self.options = options
            self.sid = options.resume or str(uuid4())
            sessions.append(self.sid)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, message):
            self.message = message

        async def call(self, name, fields):
            response = await handlers[name](fields)
            assert not response["isError"], response
            return json.loads(response["content"][0]["text"])

        async def receive_response(self):
            yield SystemMessage("init", {"session_id": self.sid})
            turns.append(self.message)
            if self.message.startswith("收到新邮件"):
                await self.call("gmail_get_message", {"message_id": "msg_invite_001"})
            elif self.message == "帮我写一封回信":
                operation.update(
                    await self.call(
                        "gmail_prepare_reply",
                        {
                            "source_message_id": "msg_invite_001",
                            "thread_id": "thread_invite_001",
                            "to": ["alice@example.com"],
                            "subject": "Re: 邀请",
                            "body": "谢谢邀请。",
                        },
                    )
                )
            elif self.message == "询问会议链接":
                current = await self.call(
                    "gmail_read_reply_draft", {"operation_id": operation["operation_id"]}
                )
                await self.call(
                    "gmail_update_reply_draft",
                    {
                        "operation_id": current["operation_id"],
                        "expected_version": current["version"],
                        "to": current["to"],
                        "subject": current["subject"],
                        "body": current["body"] + "\n请提供会议链接。",
                    },
                )
            else:
                assert '"status": "sent"' in self.options.system_prompt
            yield ResultMessage("success", 1, 1, False, 1, self.sid)

    monkeypatch.setattr(sdk_client, "QoderSDKClient", SDK)
    app = create_app(
        gateway=sdk_client.QoderGateway(tools),
        send_reply=partial(send_reply, client=gmail),
        tasks=tasks,
        drafts=drafts,
    )
    with TestClient(app) as http:
        task = http.portal.call(
            app.state.agent.accept_new_mail, "msg_invite_001", "thread_invite_001"
        )
        tid = task["task_id"]

        def wait():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = http.get(f"/api/tasks/{tid}").json()["latest_run"]
                if state and state["status"] == "done":
                    return
                assert not state or state["status"] != "error", state
                time.sleep(0.01)
            pytest.fail("后台调用未完成")

        wait()
        assert http.get(f"/api/tasks/{tid}/operations").json() == []
        duplicate = http.portal.call(
            app.state.agent.accept_new_mail, "msg_invite_001", "thread_invite_001"
        )
        assert duplicate["task_id"] == tid
        for message in ["帮我写一封回信", "询问会议链接"]:
            response = http.post(f"/api/tasks/{tid}/messages", json={"message": message})
            assert response.status_code == 202
            wait()
        oid = operation["operation_id"]
        current = drafts.get_reply_draft(oid)
        assert current["version"] == 2
        assert "会议链接" in current["body"]
        assert not gmail.sent_log
        edited = {k: current[k] for k in ("to", "subject", "body")}
        edited["body"] += "\n用户审核后的结尾  "
        response = http.patch(
            f"/api/operations/{oid}/draft", json={"expected_version": 2, **edited}
        )
        assert response.status_code == 200, response.text
        assert drafts.get_reply_draft(oid, 1)["body"] == "谢谢邀请。"
        endpoint = f"/api/tasks/{tid}/confirmations"
        assert http.post(endpoint, json={"operation_id": oid, "version": 2}).status_code == 409
        assert not gmail.sent_log
        assert http.post(endpoint, json={"operation_id": oid, "version": 3}).status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(turns) < 4:
            time.sleep(0.01)
        wait()
        assert http.post(endpoint, json={"operation_id": oid, "version": 3}).status_code == 202
        assert len(gmail.sent_log) == 1
        assert {k: gmail.sent_log[0][k] for k in edited} == edited
        assert len(set(sessions)) == 1
        assert len(turns) == 4


def test_gmail_cursor_only_advances_after_acceptance(settings):
    init_db()
    tasks = SessionStore()
    page = {
        "historyId": "12",
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"]}},
                    {"message": {"id": "sent1", "threadId": "t1", "labelIds": ["SENT"]}},
                ]
            }
        ],
    }

    class Client:
        def list_added_messages(self, history_id, page_token=None):
            return page

    source = GmailSource(Client(), path=settings.data_dir / "sync.json")
    source._save({"email": "test@example.com", "history_id": "10"})
    source.agent = SimpleNamespace(
        accept_new_mail=lambda *args: (_ for _ in ()).throw(RuntimeError("db"))
    )
    with pytest.raises(RuntimeError):
        asyncio.run(source.poll())
    assert json.loads(source.path.read_text())["history_id"] == "10"
    accepted = []
    source.agent = SimpleNamespace(accept_new_mail=lambda *args: accepted.append(args))
    asyncio.run(source.poll())
    assert accepted == [("m1", "t1")]
    assert json.loads(source.path.read_text())["history_id"] == "12"
    assert tasks.list_tasks() == []


def test_sdk_history_reads_its_persisted_transcript(settings, monkeypatch):
    from qodercn_agent_sdk import project_key_for_directory

    config = settings.data_dir / "agent" / "config"
    workspace = settings.data_dir / "agent" / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("QODERCN_CONFIG_DIR", str(config))
    project = config / "projects" / project_key_for_directory(workspace)
    project.mkdir(parents=True)
    sid, user_id, assistant_id = (str(uuid4()) for _ in range(3))
    entries = [
        {
            "uuid": user_id,
            "parentUuid": None,
            "sessionId": sid,
            "type": "user",
            "message": {"role": "user", "content": "帮我写一封回信"},
        },
        {
            "uuid": assistant_id,
            "parentUuid": user_id,
            "sessionId": sid,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "不展示"},
                    {"type": "text", "text": "草稿已准备好"},
                ],
            },
        },
    ]
    (project / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
    history = asyncio.run(
        sdk_client.QoderGateway(
            build_tools(drafts=ReplyDraftStore(), tasks=SessionStore(), gmail=MockGmailClient())
        ).read_history(task_id="task", sdk_session_id=sid)
    )
    assert history == [
        {"role": "user", "text": "帮我写一封回信"},
        {"role": "assistant", "text": "草稿已准备好"},
    ]


def test_custom_model_and_new_mail_permissions(settings, monkeypatch):
    configured = Settings(
        data_dir=settings.data_dir,
        QODERCN_PERSONAL_ACCESS_TOKEN="test-pat",
        model_provider="test-provider",
        qoder_model="test-model",
        model_api_key="test-key",
        _env_file=None,
    )
    monkeypatch.setattr(sdk_client, "get_settings", lambda: configured)
    tools = build_tools(drafts=ReplyDraftStore(), tasks=SessionStore(), gmail=MockGmailClient())
    options = sdk_client.build_options(tools, "task", allow_drafts=False)
    assert options.resolve_model(None)["model"] == {
        "provider": "test-provider",
        "model": "test-model",
        "api_key": "test-key",
        "style": "openai",
    }
    assert "test-key" not in options.system_prompt

    # 新邮件轮只分析不起草：按副作用声明排除所有写工具，只读工具照常可用。
    def names(definitions):
        return {f"mcp__pebble__{definition.name}" for definition in definitions}

    readonly = [t for t in tools if t.side_effect is SideEffect.READONLY]
    local_write = [t for t in tools if t.side_effect is SideEffect.LOCAL_WRITE]
    assert local_write, "用例前提：工具集里有本地写工具"
    assert set(options.allowed_tools) == names(readonly)
    assert not names(local_write) & set(options.allowed_tools)
    assert set(sdk_client.build_options(tools, "task").allowed_tools) == names(
        readonly + local_write
    )


def test_external_write_tools_are_never_exposed_to_the_model(settings, monkeypatch):
    """外部写工具即使注册进工具集也不会进入模型可见范围；发送只由 Confirmation 调用。"""
    monkeypatch.setattr(
        sdk_client,
        "get_settings",
        lambda: Settings(
            data_dir=settings.data_dir, QODERCN_PERSONAL_ACCESS_TOKEN="test", _env_file=None
        ),
    )
    registry = ToolRegistry()

    @registry.register(name="gmail_send_reply", side_effect=SideEffect.EXTERNAL_WRITE)
    def send_now(operation_id: str) -> dict:
        """真实发送邮件；绝不注册给模型。"""
        raise AssertionError("模型不应当能调用外部写工具")

    tools = [
        *build_tools(drafts=ReplyDraftStore(), tasks=SessionStore(), gmail=MockGmailClient()),
        send_now,
    ]
    for allow_drafts in (True, False):
        options = sdk_client.build_options(tools, "task", allow_drafts=allow_drafts)
        assert not any("send" in name for name in options.allowed_tools)
    assert send_now not in sdk_client.exposed_tools(tools, allow_drafts=True)


def test_tools_bind_assembled_dependencies_without_global_state(settings, tmp_path):
    """两套装配各自持有存储：工具不查全局，同一进程可并存互不影响的应用。"""
    toolsets = []
    for name in ("left", "right"):
        path = tmp_path / f"{name}.db"
        init_db(path)
        tasks = SessionStore(path)
        drafts = ReplyDraftStore(path)
        task_id = tasks.create_task(f"{name} 的任务")["task_id"]
        tools = {
            definition.name: definition
            for definition in build_tools(drafts=drafts, tasks=tasks, gmail=MockGmailClient())
        }
        toolsets.append((name, task_id, drafts, tools))

    saved = []
    for name, task_id, _, tools in toolsets:
        saved.append(
            tools["gmail_prepare_reply"](
                task_id=task_id,
                source_message_id="msg_invite_001",
                thread_id="thread_invite_001",
                to=["alice@example.com"],
                subject="Re: 邀请",
                body=f"{name} 的草稿。",
            )
        )

    # 同一原邮件在两套装配里各自建立操作，说明去重作用于各自的存储而不是进程全局。
    assert saved[0]["operation_id"] != saved[1]["operation_id"]
    for (name, _, drafts, _), result in zip(toolsets, saved, strict=True):
        assert drafts.get_reply_draft(result["operation_id"])["body"] == f"{name} 的草稿。"
