"""Gmail 只读工具的统一输出与附件交付。"""

from server.tools.gmail.tools import (
    get_attachment,
    get_email_detail,
    get_email_thread,
    search_emails,
)
from server.tools.registry import SideEffect, ToolFileResult, default_registry
from tests.support.gmail_double import MockGmailClient


def test_gmail_tools_registered_as_readonly() -> None:
    for name in ("gmail_search", "gmail_get_thread", "gmail_get_message", "gmail_get_attachment"):
        definition = default_registry.get_tool(name)
        assert definition is not None
        assert definition.side_effect == SideEffect.READONLY


def test_search_returns_uniform_message_summaries() -> None:
    results = search_emails(query="评审", gmail=MockGmailClient())
    assert len(results) == 1
    assert results[0] == {
        "message_id": "msg_invite_001",
        "thread_id": "thread_invite_001",
        "rfc_message_id": "<invite-001@example.com>",
        "from": "Alice <alice@example.com>",
        "to": ["user@example.com"],
        "cc": [],
        "subject": "项目进展评审与架构讨论邀请",
        "snippet": "诚邀您参加下周二下午 2 点的项目进展评审会议...",
        "received_at": "2026-09-12T10:00:00+08:00",
        "attachments": [
            {
                "attachment_id": "attachment_invite_001",
                "filename": "会议说明.txt",
                "mime_type": "text/plain",
                "size": 30,
            }
        ],
    }


def test_get_thread_returns_each_message_once() -> None:
    gmail = MockGmailClient()
    gmail.raw_send_message(
        to=["alice@example.com"],
        subject="Re: 项目进展评审与架构讨论邀请",
        body="确认可以按时出席会议。",
        thread_id="thread_invite_001",
        in_reply_to_rfc_id="<invite-001@example.com>",
    )
    result = get_email_thread(thread_id="thread_invite_001", gmail=gmail)
    assert set(result) == {"thread_id", "messages"}
    assert [item["message_id"] for item in result["messages"]] == [
        "msg_invite_001",
        gmail.sent_log[0]["id"],
    ]
    assert result["messages"][1]["body"] == "确认可以按时出席会议。"


def test_get_message_includes_attachment_metadata() -> None:
    detail = get_email_detail(message_id="msg_invite_001", gmail=MockGmailClient())
    assert detail["message_id"] == "msg_invite_001"
    assert detail["to"] == ["user@example.com"]
    assert "第二会议室" in detail["body"]
    assert detail["attachments"][0]["attachment_id"] == "attachment_invite_001"


def test_get_attachment_returns_original_file() -> None:
    result = get_attachment(
        message_id="msg_invite_001",
        attachment_id="attachment_invite_001",
        gmail=MockGmailClient(),
    )
    assert isinstance(result, ToolFileResult)
    assert result.filename == "会议说明.txt"
    assert result.mime_type == "text/plain"
    assert result.data == "请提前准备项目进展。".encode()
