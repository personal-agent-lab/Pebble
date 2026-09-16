"""模块集成验证：真实业务模块、SQLite、HTTP；只替换 SDK 子进程和 Gmail 的外部边界。"""

import asyncio
import json
import time
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from qodercn_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from server.agent import client as agent_client
from server.agent.client import QoderGateway
from server.agent.mcp import TOOL_SERVER_NAME, ToolServer
from server.agent.prompt import TITLE_PROMPT
from server.agent.toolset import ToolDeps, build_tools
from server.config import Settings
from server.db import init_db
from server.main import create_app
from server.memory.judge import JUDGE_INSTRUCTIONS
from server.sessions.service import SessionStore
from server.tools.gmail.sender import send_message
from server.tools.gmail.service import MailDraftStore
from server.tools.gmail.sync import GmailSource
from tests.support.gmail_double import MockGmailClient
from tests.support.mcp_http import mcp_session, tool_payload


def test_sdk_tools_to_http_confirmation(settings, monkeypatch):
    """从新邮件分析到用户确认发送：脚本化 SDK 经工具端点调用真实工具，其余全是真的。"""
    init_db()
    tasks = SessionStore()
    drafts = MailDraftStore()
    gmail = MockGmailClient()
    tool_server = ToolServer()
    gateway = QoderGateway(
        ToolDeps(drafts=drafts, tasks=tasks, gmail=gmail),
        tool_server,
        settings=Settings(
            data_dir=settings.data_dir, QODERCN_PERSONAL_ACCESS_TOKEN="test", _env_file=None
        ),
    )
    sessions = []
    turns = []
    operation = {}

    class SDK:
        """脚本化 SDK：按消息内容决定调用哪些工具，模型消息由脚本给出。"""

        def __init__(self, options):
            self.options = options
            self.sid = options.resume or str(uuid4())
            self.is_title = options.system_prompt == TITLE_PROMPT
            # 每轮记忆判断是独立的一次性调用：不登记会话、不进脚本分支，直接结束。
            self.is_judge = options.system_prompt == JUDGE_INSTRUCTIONS
            if not self.is_title and not self.is_judge:
                sessions.append(self.sid)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, message):
            self.message = message

        async def get_context_usage(self):
            return {
                "contextWindow": {"usedPercentage": 20},
                "autoCompact": {"enabled": True, "thresholdPercentage": 80},
            }

        async def call(self, name, fields):
            url = self.options.mcp_servers[TOOL_SERVER_NAME]["url"]
            async with mcp_session(app, url) as session:
                result = await session.call_tool(name, fields)
            assert result.isError is False, result
            return tool_payload(result)

        def say(self, text):
            return AssistantMessage([TextBlock(text)], "model", session_id=self.sid)

        async def receive_response(self):
            yield SystemMessage("init", {"session_id": self.sid})
            if self.is_judge:
                yield ResultMessage("success", 1, 1, False, 1, self.sid)
                return
            if self.is_title:
                yield self.say("邀请回复")
                yield ResultMessage("success", 1, 1, False, 1, self.sid)
                return
            turns.append(self.message)
            if self.message.startswith("收到新邮件"):
                message = await self.call("gmail_get_message", {"message_id": "msg_invite_001"})
                yield self.say(f"邮件主题：{message['subject']}")
            elif self.message == "帮我写一封回信":
                operation.update(
                    await self.call(
                        "gmail_prepare_reply",
                        {
                            "source_message_id": "msg_invite_001",
                            "to": ["alice@example.com"],
                            "subject": "Re: 邀请",
                            "body": "谢谢邀请。",
                        },
                    )
                )
                yield self.say("草稿已保存，请审核。")
            elif self.message == "询问会议链接":
                current = await self.call(
                    "gmail_read_draft", {"operation_id": operation["operation_id"]}
                )
                await self.call(
                    "gmail_update_draft",
                    {
                        "operation_id": current["operation_id"],
                        "expected_version": current["version"],
                        "to": current["to"],
                        "subject": current["subject"],
                        "body": current["body"] + "\n请提供会议链接。",
                    },
                )
                yield self.say("已按你的要求补充。")
            else:
                matcher = self.options.hooks["SessionStart"][0]
                hook_result = await matcher.hooks[0]({}, None, {})
                assert '"status": "sent"' in hook_result["hookSpecificOutput"]["additionalContext"]
                yield self.say("邮件已经发出去了。")
            yield ResultMessage("success", 1, 1, False, 1, self.sid)

    monkeypatch.setattr(agent_client, "QoderSDKClient", SDK)
    app = create_app(
        gateway=gateway,
        send_message=partial(send_message, client=gmail),
        tasks=tasks,
        drafts=drafts,
        tool_server=tool_server,
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
        current = drafts.get_draft(oid)
        assert current["version"] == 2
        assert "会议链接" in current["body"]
        assert not gmail.sent_log
        edited = {key: current[key] for key in ("to", "subject", "body")}
        edited["body"] += "\n用户审核后的结尾  "
        response = http.patch(
            f"/api/operations/{oid}/draft",
            json={"expected_version": 2, **edited},
        )
        assert response.status_code == 200, response.text
        assert drafts.get_draft(oid, 1)["body"] == "谢谢邀请。"
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
        assert {key: gmail.sent_log[0][key] for key in edited} == edited
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


def test_tools_bind_assembled_dependencies_without_global_state(settings, tmp_path):
    """两套装配各自持有存储：工具不查全局，同一进程可并存互不影响的应用。"""
    toolsets = []
    for name in ("left", "right"):
        path = tmp_path / f"{name}.db"
        init_db(path)
        tasks = SessionStore(path)
        drafts = MailDraftStore(path)
        task_id = tasks.create_task(f"{name} 的任务")["task_id"]
        tools = {
            definition.name: definition
            for definition in build_tools(
                ToolDeps(drafts=drafts, tasks=tasks, gmail=MockGmailClient())
            )
        }
        toolsets.append((name, task_id, drafts, tools))

    saved = []
    for name, task_id, _, tools in toolsets:
        saved.append(
            tools["gmail_prepare_reply"](
                source_message_id="msg_invite_001",
                to=["alice@example.com"],
                subject="Re: 邀请",
                body=f"{name} 的草稿。",
                task_id=task_id,
            )
        )

    # 同一原邮件在两套装配里各自建立操作，说明去重作用于各自的存储而不是进程全局。
    assert saved[0]["operation_id"] != saved[1]["operation_id"]
    for (name, _, drafts, _), result in zip(toolsets, saved, strict=True):
        assert drafts.get_draft(result["operation_id"])["body"] == f"{name} 的草稿。"
