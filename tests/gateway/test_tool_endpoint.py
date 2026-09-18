"""工具端点：每一轮按一次性令牌登记当轮工具，模型只能看到并调用这些工具。"""

import asyncio
import base64

import httpx

from server.agent.mcp import ToolServer
from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, build_tools, exposed_tools
from server.db import init_db, session, write
from server.memory.service import MemoryStore
from server.sessions.repository import cancel_pending
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.memory.tools import judge_registry, review_registry
from server.tools.personal_kb.service import KbStore
from tests.support import memory_anchor, seed_memory
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
    tools = build_tools(
        ToolDeps(
            drafts=MailDraftStore(),
            tasks=tasks,
            gmail=MockGmailClient(),
            kb_store=KbStore(settings.data_dir),
        )
    )
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


def test_kb_save_emits_program_notice_without_recording_source(settings):
    tools, tasks = build(settings)
    task_id = tasks.create_task("保存资料")["task_id"]
    server = ToolServer()
    visible = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
    queued: asyncio.Queue = asyncio.Queue()

    async def scenario():
        async with (
            server.serve(visible, task_id=task_id, queued=queued) as path,
            mcp_session(server, f"{BASE_URL}{path}") as session,
        ):
            saved = await session.call_tool(
                "kb_save", {"title": "会议纪要", "body": "结论为通过。"}
            )
            payload = tool_payload(saved)
            notice = queued.get_nowait()
            return payload, notice

    payload, notice = asyncio.run(scenario())
    assert notice == {
        "type": "notice",
        "text": "已保存资料：会议纪要。位置：资料库根目录",
    }
    store = KbStore(settings.data_dir)
    assert "source" not in store.read(path=payload["path"])
    assert "source:" not in (settings.data_dir / payload["path"]).read_text(encoding="utf-8")


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


def test_update_of_cancelled_draft_creates_new_one(settings):
    """对话轮开始时草稿已取消：按意见保存时以它为底另起新草稿，新卡片由 draft_saved 带出。"""
    tools, tasks = build(settings)
    drafts = MailDraftStore()
    task_id = tasks.create_task("写一封通知")["task_id"]
    original = drafts.save_email_draft(task_id, ["a@example.com"], "主题", "正文")
    with session() as conn, write(conn):
        cancel_pending(conn, task_id, "2026-09-18T00:00:00Z")
    server = ToolServer()
    drafting = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
    queued: asyncio.Queue = asyncio.Queue()
    revision = {
        "operation_id": original["operation_id"],
        "expected_version": 1,
        "to": ["a@example.com"],
        "subject": "更正式的主题",
        "body": "正文",
    }

    async def scenario():
        async with (
            server.serve(drafting, task_id=task_id, queued=queued) as path,
            mcp_session(server, f"{BASE_URL}{path}") as session_,
        ):
            first = tool_payload(await session_.call_tool("gmail_update_draft", revision))
            # 同一轮再改新草稿：它还是待确认，原地另存一版，不再另起卡片。
            again = tool_payload(
                await session_.call_tool(
                    "gmail_update_draft",
                    {**revision, "operation_id": first["operation_id"], "body": "正文二"},
                )
            )
            return first, again

    first, again = asyncio.run(scenario())
    assert first["operation_id"] != original["operation_id"]
    assert (first["version"], first["status"]) == (1, "pending")
    assert queued.get_nowait()["operation_id"] == first["operation_id"]
    assert (again["operation_id"], again["version"]) == (first["operation_id"], 2)
    old = drafts.get_draft(original["operation_id"])
    assert (old["status"], old["version"], old["subject"]) == ("cancelled", 1, "主题")
    assert drafts.get_draft(first["operation_id"])["body"] == "正文二"
    linked = {item["operation_id"] for item in tasks.list_task_operations(task_id)}
    assert linked == {original["operation_id"], first["operation_id"]}


def test_review_endpoint_exposes_review_tools(settings):
    init_db()
    store = MemoryStore(settings.data_dir)
    tools = build_tools(
        ToolDeps(drafts=None, tasks=None, gmail=None, memory_store=store),
        registry=review_registry,
    )
    task_id = SessionStore().create_task("闲聊")["task_id"]
    server = ToolServer()

    async def scenario():
        async with (
            server.serve(tools, task_id=task_id, queued=asyncio.Queue()) as path,
            mcp_session(server, f"{BASE_URL}{path}") as session,
        ):
            listed = await session.list_tools()
            assert [tool.name for tool in listed.tools] == ["memory_edit"]
            operations = listed.tools[0].inputSchema["properties"]["operations"]
            assert operations["items"]["properties"]["action"]["enum"] == [
                "append",
                "insert",
                "replace",
                "delete",
                "move",
            ]

            saved = await session.call_tool(
                "memory_edit",
                {
                    "operations": [
                        {"action": "append", "target": "user", "text": "用户在研究记忆机制"}
                    ]
                },
            )
            assert saved.isError is False
            assert tool_payload(saved)["changed"] is True

            stale = await session.call_tool(
                "memory_edit", {"operations": [{"action": "delete", "anchor": "zzzz"}]}
            )
            assert stale.isError is True
            assert "用户在研究记忆机制" in tool_payload(stale)["memory"]["user"]["content"]

    asyncio.run(scenario())
    assert store.snapshot()["user"]["content"] == "用户在研究记忆机制"


def test_judge_endpoint_exposes_judgment_tools(settings):
    init_db()
    store = MemoryStore(settings.data_dir)
    seed_memory(store, "user", "用户在研究记忆机制")
    line = memory_anchor(store, "用户在研究记忆机制")
    tools = build_tools(
        ToolDeps(drafts=None, tasks=None, gmail=None, memory_store=store),
        registry=judge_registry,
    )
    task_id = SessionStore().create_task("闲聊")["task_id"]
    server = ToolServer()

    async def scenario():
        async with (
            server.serve(tools, task_id=task_id, queued=asyncio.Queue()) as path,
            mcp_session(server, f"{BASE_URL}{path}") as session,
        ):
            listed = await session.list_tools()
            assert [tool.name for tool in listed.tools] == ["memory_edit"]

            replaced = await session.call_tool(
                "memory_edit",
                {
                    "operations": [
                        {"action": "replace", "anchor": line, "text": "用户在研究长期记忆机制"}
                    ]
                },
            )
            assert replaced.isError is False
            assert tool_payload(replaced)["changed"] is True

            # 已退役的前台 memory 工具在判断会话不存在。
            blocked = await session.call_tool(
                "memory", {"action": "add", "target": "user", "content": "x"}
            )
            assert blocked.isError is True
            assert tool_payload(blocked)["error"] == "unknown_tool"

    asyncio.run(scenario())
    assert store.snapshot()["user"]["content"] == "用户在研究长期记忆机制"


def test_kb_destructive_tools_emit_notices_and_trigger_turns_can_only_save(settings):
    tools, tasks = build(settings)
    task_id = tasks.create_task("整理资料")["task_id"]
    message_tools = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
    trigger_tools = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.NEW_MAIL])

    async def call(visible, name, arguments):
        server = ToolServer()
        queued: asyncio.Queue = asyncio.Queue()
        async with (
            server.serve(visible, task_id=task_id, queued=queued) as path,
            mcp_session(server, f"{BASE_URL}{path}") as session,
        ):
            result = await session.call_tool(name, arguments)
        notices = []
        while not queued.empty():
            notices.append(queued.get_nowait()["text"])
        return result, notices

    async def scenario():
        # 新邮件轮可以保存资料，但拿不到删除
        saved, notices = await call(
            trigger_tools, "kb_save", {"title": "邀请约定", "body": "周四下午三点讨论。"}
        )
        payload = tool_payload(saved)
        assert notices == ["已保存资料：邀请约定。位置：资料库根目录"]
        refused, _ = await call(
            trigger_tools,
            "kb_delete",
            {"expected_version": payload["version"], "id": payload["id"]},
        )
        assert refused.isError is True
        assert tool_payload(refused)["error"] == "unknown_tool"

        moved, notices = await call(
            message_tools,
            "kb_move",
            {"expected_version": payload["version"], "id": payload["id"], "new_path": "课程/约定"},
        )
        moved_payload = tool_payload(moved)
        assert notices == ["已移动资料：邀请约定。资料库根目录 → 「课程」文件夹"]

        deleted, notices = await call(
            message_tools,
            "kb_delete",
            {"expected_version": moved_payload["version"], "id": payload["id"]},
        )
        assert tool_payload(deleted)["path"] == "kb/课程/约定.md"
        assert notices == [
            "已删除资料：邀请约定。原位置：「课程」文件夹。历史版本仍保留，可以恢复"
        ]

        restored, notices = await call(
            message_tools, "kb_restore", {"id": payload["id"], "version": payload["version"]}
        )
        restored_payload = tool_payload(restored)
        assert restored_payload["path"] == payload["path"]
        assert notices == [
            f"已恢复资料：邀请约定。位置：资料库根目录。恢复自版本：{payload['version']}"
        ]

    asyncio.run(scenario())
