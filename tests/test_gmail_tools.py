"""tests/test_gmail_tools.py: 测试 Gmail 只读查询工具与注册属性。"""

from server.tools.gmail.client import MockGmailClient
from server.tools.gmail.tools import (
    get_email_detail,
    get_email_thread,
    query_emails,
)
from server.tools.registry import SideEffect, default_registry


def test_gmail_tools_registered_as_readonly() -> None:
    tool_names = ["gmail_query_emails", "gmail_get_thread", "gmail_get_message"]
    for name in tool_names:
        t = default_registry.get_tool(name)
        assert t is not None
        assert t.side_effect == SideEffect.READONLY
        assert t.description != ""


def test_query_emails_returns_summaries() -> None:
    client = MockGmailClient()
    results = query_emails(query="评审", client=client)

    assert len(results) == 1
    item = results[0]
    assert item["id"] == "msg_invite_001"
    assert item["thread_id"] == "thread_invite_001"
    assert item["from"] == "Alice <alice@example.com>"
    assert "架构讨论" in item["subject"]
    assert "诚邀" in item["snippet"]


def test_get_email_thread_returns_thread_context() -> None:
    client = MockGmailClient()
    thread = get_email_thread(thread_id="thread_invite_001", client=client)

    assert len(thread) == 1
    msg = thread[0]
    assert msg["id"] == "msg_invite_001"
    assert msg["rfc_message_id"] == "<invite-001@example.com>"
    assert "第二会议室" in msg["body"]


def test_get_email_detail_returns_single_message() -> None:
    client = MockGmailClient()
    detail = get_email_detail(message_id="msg_invite_001", client=client)

    assert detail["id"] == "msg_invite_001"
    assert detail["from"] == "Alice <alice@example.com>"
    assert detail["to"] == ["user@example.com"]
    assert "第二会议室" in detail["body"]
