"""tests/test_gmail_tools.py: 测试 Gmail 只读查询工具与注册属性。"""

from server.tools.gmail.client import MockGmailClient
from server.tools.gmail.tools import (
    format_thread_transcript,
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


def test_get_email_thread_returns_structured_and_transcript_context() -> None:
    client = MockGmailClient()
    # 模拟在线程内已有一轮回复
    client.raw_send_reply(
        to=["alice@example.com"],
        subject="Re: 项目进展评审与架构讨论邀请",
        body="确认可以按时出席会议。",
        thread_id="thread_invite_001",
        in_reply_to_rfc_id="<invite-001@example.com>",
    )

    thread_data = get_email_thread(thread_id="thread_invite_001", client=client)

    assert thread_data["thread_id"] == "thread_invite_001"
    assert thread_data["total_messages"] == 2

    # 验证结构化列表按顺序排序
    msgs = thread_data["messages"]
    assert len(msgs) == 2
    assert msgs[0]["sequence"] == 1
    assert msgs[0]["id"] == "msg_invite_001"
    assert msgs[1]["sequence"] == 2
    assert msgs[1]["id"].startswith("mock_sent_")

    # 验证时间线文本（注入模型上下文）
    transcript = thread_data["transcript"]
    assert "共 2 封" in transcript
    assert "【第 1 封往来" in transcript
    assert "发件人: Alice <alice@example.com>" in transcript
    assert "【第 2 封往来" in transcript
    assert "确认可以按时出席会议。" in transcript


def test_format_thread_transcript_empty() -> None:
    assert "暂无" in format_thread_transcript([])


def test_get_email_detail_returns_single_message() -> None:
    client = MockGmailClient()
    detail = get_email_detail(message_id="msg_invite_001", client=client)

    assert detail["id"] == "msg_invite_001"
    assert detail["from"] == "Alice <alice@example.com>"
    assert detail["to"] == ["user@example.com"]
    assert "第二会议室" in detail["body"]
