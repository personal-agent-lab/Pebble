"""工具端点：每一轮按一次性令牌登记当轮工具，模型只能看到并调用这些工具。"""

import asyncio
import base64

import httpx

from server.agent.mcp import ToolServer
from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, build_tools, exposed_tools
from server.db import init_db
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient
from tests.support.mcp_http import mcp_session, tool_payload

BASE_URL = "http://127.0.0.1:8000"
DRAFT = {
    "source_message_id": "msg_invite_001",
    "to": ["alice@example.com"],
    "subject": "Re: 邀请",
    "body": "谢谢邀请，我准时参加。",
}


def build(settings):
    init_db()
    tasks = SessionStore()
    tools = build_tools(ToolDeps(drafts=MailDraftStore(), tasks=tasks, gmail=MockGmailClient()))
    return tools, tasks


async def status(app, url: str) -> int:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        return (await client.post(url, json={})).status_code


def test_turn_endpoint_exposes_only_its_own_tools(settings):
    tools, tasks = build(settings)
    task_id = tasks.create_task("处理新收到的邮件")["task_id"]
    server = ToolServer()
    visible = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.NEW_MAIL])

    async def scenario():
        async with server.serve(visible, task_id=task_id, queued=asyncio.Queue()) as path:
            url = f"{BASE_URL}{path}"
            async with mcp_session(server, url) as session:
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == [tool.name for tool in visible]

                read = await session.call_tool(
                    "gmail_get_message", {"message_id": "msg_invite_001"}
                )
                assert read.isError is False
                assert tool_payload(read)["subject"]

                attachment = await session.call_tool(
                    "gmail_get_attachment",
                    {
                        "message_id": "msg_invite_001",
                        "attachment_id": "attachment_invite_001",
                    },
                )
                assert attachment.isError is False
                assert tool_payload(attachment)["filename"] == "会议说明.txt"
                assert attachment.content[1].resource.mimeType == "text/plain"
                assert base64.b64decode(attachment.content[1].resource.blob) == (
                    "请提前准备项目进展。".encode()
                )

                # 同名工具在别的轮次可见，本轮调用按不存在处理，不解释原因。
                blocked = await session.call_tool("gmail_prepare_reply", dict(DRAFT))
                assert blocked.isError is True
                assert tool_payload(blocked)["error"] == "unknown_tool"
            return path

    path = asyncio.run(scenario())
    # 轮次结束即撤销：旧令牌不再指向任何工具。
    assert asyncio.run(status(server, f"{BASE_URL}{path}")) == 404
    assert asyncio.run(status(server, f"{BASE_URL}/mcp/never-issued")) == 404


def test_concurrent_turns_are_isolated(settings):
    tools, tasks = build(settings)
    task_id = tasks.create_task("处理新收到的邮件")["task_id"]
    server = ToolServer()
    drafting = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
    readonly = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.NEW_MAIL])

    async def scenario():
        mail_queue: asyncio.Queue = asyncio.Queue()
        draft_queue: asyncio.Queue = asyncio.Queue()
        async with (
            server.serve(readonly, task_id=task_id, queued=mail_queue) as mail_path,
            server.serve(drafting, task_id=task_id, queued=draft_queue) as draft_path,
        ):
            assert mail_path != draft_path
            async with (
                mcp_session(server, f"{BASE_URL}{mail_path}") as mail,
                mcp_session(server, f"{BASE_URL}{draft_path}") as draft,
            ):
                mail_tools = {tool.name for tool in (await mail.list_tools()).tools}
                draft_tools = {tool.name for tool in (await draft.list_tools()).tools}
                assert "gmail_prepare_reply" in draft_tools
                assert "gmail_prepare_reply" not in mail_tools

                saved = await draft.call_tool("gmail_prepare_reply", dict(DRAFT))
                assert saved.isError is False
                assert tool_payload(saved)["version"] == 1
                # 草稿事件只进产生它的那一轮。
                assert draft_queue.get_nowait()["type"] == "draft_saved"
                assert mail_queue.empty()

    asyncio.run(scenario())


def test_targeted_turn_can_only_update_selected_draft(settings):
    tools, tasks = build(settings)
    task_id = tasks.create_task("修改邮件")["task_id"]
    drafts = MailDraftStore()
    target = drafts.save_email_draft(task_id, ["a@example.com"], "主题", "正文")
    other = drafts.save_email_draft(task_id, ["b@example.com"], "其他", "正文")
    server = ToolServer()
    drafting = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])

    async def scenario():
        async with (
            server.serve(
                drafting,
                task_id=task_id,
                target_operation_id=target["operation_id"],
                queued=asyncio.Queue(),
            ) as path,
            mcp_session(server, f"{BASE_URL}{path}") as session,
        ):
            created = await session.call_tool(
                "gmail_prepare_email",
                {
                    "to": ["c@example.com"],
                    "subject": "新建",
                    "body": "正文",
                },
            )
            assert tool_payload(created)["error"] == "wrong_target"
            wrong = await session.call_tool(
                "gmail_read_draft", {"operation_id": other["operation_id"]}
            )
            assert tool_payload(wrong)["error"] == "wrong_target"
            updated = await session.call_tool(
                "gmail_update_draft",
                {
                    "operation_id": target["operation_id"],
                    "expected_version": 1,
                    "to": ["a@example.com"],
                    "subject": "已更新",
                    "body": "正文",
                },
            )
            assert tool_payload(updated)["version"] == 2

    asyncio.run(scenario())
