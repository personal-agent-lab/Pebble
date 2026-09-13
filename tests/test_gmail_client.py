"""tests/test_gmail_client.py: 测试 GmailClient 协议实现、Mock 回退与 MIME 报文解析。"""

import base64
import email
from unittest.mock import MagicMock

import pytest

from server.config import Settings
from server.tools.gmail.client import (
    GoogleApiGmailClient,
    MimeParser,
    MockGmailClient,
    get_gmail_client,
)


def test_mock_client_get_message_and_thread() -> None:
    client = MockGmailClient()
    msg = client.get_message("msg_invite_001")

    assert msg.id == "msg_invite_001"
    assert msg.thread_id == "thread_invite_001"
    assert msg.from_addr == "Alice <alice@example.com>"
    assert "架构讨论" in msg.subject
    assert "第二会议室" in msg.body_text
    assert msg.internal_date_ms > 0
    assert "INBOX" in msg.labels

    thread = client.get_thread("thread_invite_001")
    assert len(thread) == 1
    assert thread[0].id == "msg_invite_001"


def test_mock_client_message_not_found() -> None:
    client = MockGmailClient()
    with pytest.raises(KeyError):
        client.get_message("non_existent_id")


def test_mock_client_search_messages() -> None:
    client = MockGmailClient()
    results = client.search_messages("第二会议室")
    assert len(results) == 1
    assert results[0]["id"] == "msg_invite_001"

    empty_results = client.search_messages("不存在的内容")
    assert len(empty_results) == 0


def test_mock_client_raw_send_reply_and_verify() -> None:
    client = MockGmailClient()
    res = client.raw_send_reply(
        to=["alice@example.com"],
        subject="Re: 项目进展评审与架构讨论邀请",
        body="确认可以按时出席会议。",
        thread_id="thread_invite_001",
        in_reply_to_rfc_id="<invite-001@example.com>",
    )

    assert res.thread_id == "thread_invite_001"
    assert res.message_id.startswith("mock_sent_")

    # 验证 thread 内追加了回复
    thread = client.get_thread("thread_invite_001")
    assert len(thread) == 2
    assert thread[1].id == res.message_id
    assert thread[1].body_text == "确认可以按时出席会议。"
    assert "SENT" in thread[1].labels

    # 验证发件状态核实
    assert client.verify_message_sent("thread_invite_001", "项目进展评审与架构讨论邀请")
    assert not client.verify_message_sent("thread_invite_001", "完全不相关的邮件")


def test_mime_parser_helpers() -> None:
    # 1. 测试 base64url 解码与缺少 padding 补全
    raw_str = "测试数据"
    b64_unpadded = base64.urlsafe_b64encode(raw_str.encode("utf-8")).decode("ascii").rstrip("=")
    assert MimeParser.safe_b64decode(b64_unpadded) == raw_str
    assert MimeParser.safe_b64decode("") == ""

    # 2. 测试 RFC 2047 编码头解码
    encoded_subj = "=?utf-8?B?5Lya6K6u56Gu6K6k?="
    assert MimeParser.decode_header_value(encoded_subj) == "会议确认"

    # 3. 测试收件人解析（带姓名与逗号）
    addr_header = (
        '"Alice Smith" <alice@example.com>, bob@example.com, Charlie <charlie@example.com>'
    )
    addrs = MimeParser.parse_address_list(addr_header)
    assert addrs == ["alice@example.com", "bob@example.com", "charlie@example.com"]

    # 4. 测试 HTML 清洗
    raw_html = "<p>您好：<br>欢迎参加会议！&nbsp;&amp;&nbsp;讨论。</p><div>详情见附件</div>"
    cleaned = MimeParser.strip_html_tags(raw_html)
    assert "您好：" in cleaned
    assert "欢迎参加会议！ & 讨论。" in cleaned
    assert "详情见附件" in cleaned
    assert "<p>" not in cleaned


def test_google_api_client_parsing_and_mime() -> None:
    mock_service = MagicMock()
    client = GoogleApiGmailClient(
        credentials_path=MagicMock(),
        token_path=MagicMock(),
        service=mock_service,
    )

    encoded_body = base64.urlsafe_b64encode("这是测试正文内容".encode()).decode("ascii")
    raw_payload = {
        "id": "18f001",
        "threadId": "thread_18f001",
        "snippet": "测试摘要",
        "internalDate": "1789200000000",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Message-ID", "value": "<test-18f001@mail.gmail.com>"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "Reply-To", "value": "replies@example.net"},
                {"name": "To", "value": "recipient1@example.com, recipient2@example.com"},
                {"name": "Cc", "value": "manager@example.com"},
                {"name": "Subject", "value": "=?utf-8?B?6L+b5bqm5ZCM5q2l?="},
                {"name": "Date", "value": "Sat, 12 Sep 2026 12:00:00 +0000"},
            ],
            "body": {
                "data": encoded_body,
            },
        },
    }

    mock_service.users().messages().get().execute.return_value = raw_payload

    msg = client.get_message("18f001")
    assert msg.id == "18f001"
    assert msg.thread_id == "thread_18f001"
    assert msg.rfc_message_id == "<test-18f001@mail.gmail.com>"
    assert msg.from_addr == "sender@example.com"
    assert msg.reply_to_addrs == ["replies@example.net"]
    assert msg.to_addrs == ["recipient1@example.com", "recipient2@example.com"]
    assert msg.cc_addrs == ["manager@example.com"]
    assert msg.subject == "进度同步"  # 验证 RFC 2047 解码成功
    assert msg.body_text == "这是测试正文内容"
    assert msg.internal_date_ms == 1789200000000
    assert msg.labels == ["INBOX", "UNREAD"]


def test_google_api_client_html_only_fallback() -> None:
    mock_service = MagicMock()
    client = GoogleApiGmailClient(
        credentials_path=MagicMock(),
        token_path=MagicMock(),
        service=mock_service,
    )

    encoded_html = base64.urlsafe_b64encode("<p>这是来自纯 HTML 模板的通知。</p>".encode()).decode(
        "ascii"
    )

    raw_payload = {
        "id": "18f003",
        "threadId": "thread_18f003",
        "snippet": "HTML 摘要",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "Subject", "value": "HTML 邮件测试"},
            ],
            "body": {
                "data": encoded_html,
            },
        },
    }

    mock_service.users().messages().get().execute.return_value = raw_payload
    msg = client.get_message("18f003")

    # 验证降级清洗为纯文本
    assert msg.body_text == "这是来自纯 HTML 模板的通知。"
    assert "<p>" not in msg.body_text
    assert "<p>这是来自纯 HTML 模板的通知。</p>" in msg.body_html


def test_google_api_client_thread_chronological_sorting() -> None:
    mock_service = MagicMock()
    client = GoogleApiGmailClient(
        credentials_path=MagicMock(),
        token_path=MagicMock(),
        service=mock_service,
    )

    # 模拟两封乱序返回的邮件：msg2 发生较早，msg1 发生较晚
    raw_msg1 = {
        "id": "msg_002",
        "threadId": "t100",
        "internalDate": "1789200200000",
        "payload": {
            "headers": [{"name": "Subject", "value": "第二封邮件"}],
            "body": {"data": base64.urlsafe_b64encode("第二封正文".encode()).decode("ascii")},
        },
    }
    raw_msg2 = {
        "id": "msg_001",
        "threadId": "t100",
        "internalDate": "1789200100000",
        "payload": {
            "headers": [{"name": "Subject", "value": "第一封邮件"}],
            "body": {"data": base64.urlsafe_b64encode("第一封正文".encode()).decode("ascii")},
        },
    }

    # Gmail API 乱序返回 [msg1, msg2]
    mock_service.users().threads().get().execute.return_value = {
        "id": "t100",
        "messages": [raw_msg1, raw_msg2],
    }

    thread = client.get_thread("t100")
    assert len(thread) == 2
    # 验证按 internalDate 升序排序
    assert thread[0].id == "msg_001"
    assert thread[0].subject == "第一封邮件"
    assert thread[1].id == "msg_002"
    assert thread[1].subject == "第二封邮件"


def test_google_api_client_nested_multipart_parsing() -> None:
    mock_service = MagicMock()
    client = GoogleApiGmailClient(
        credentials_path=MagicMock(),
        token_path=MagicMock(),
        service=mock_service,
    )

    encoded_plain = base64.urlsafe_b64encode("嵌套纯文本正文".encode()).decode("ascii")
    raw_payload = {
        "id": "18f002",
        "threadId": "thread_18f002",
        "snippet": "嵌套摘要",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "多层结构测试"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": encoded_plain},
                        },
                        {
                            "mimeType": "text/html",
                            "body": {"data": "PGh0bWw+PC9odG1sPg=="},
                        },
                    ],
                }
            ],
        },
    }

    mock_service.users().messages().get().execute.return_value = raw_payload
    msg = client.get_message("18f002")
    assert msg.body_text == "嵌套纯文本正文"


def test_google_api_client_raw_send_reply_headers() -> None:
    mock_service = MagicMock()
    client = GoogleApiGmailClient(
        credentials_path=MagicMock(),
        token_path=MagicMock(),
        service=mock_service,
    )

    mock_service.users().messages().send().execute.return_value = {
        "id": "sent_999",
        "threadId": "thread_orig_123",
    }

    res = client.raw_send_reply(
        to=["target@example.com", "cc@example.com"],
        subject="Re: 会议确认",
        body="收到，准时参加。",
        thread_id="thread_orig_123",
        in_reply_to_rfc_id="<orig-rfc-id@example.com>",
    )

    assert res.message_id == "sent_999"
    assert res.thread_id == "thread_orig_123"

    send_call = mock_service.users().messages().send.call_args
    assert send_call is not None
    body_sent = send_call.kwargs["body"]
    assert body_sent["threadId"] == "thread_orig_123"

    # 解码 raw 验证 RFC 2822 邮件头
    raw_bytes = base64.urlsafe_b64decode(body_sent["raw"].encode("ascii"))
    parsed_msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    assert parsed_msg["To"] == "target@example.com, cc@example.com"
    assert parsed_msg["Subject"] == "Re: 会议确认"
    assert parsed_msg["In-Reply-To"] == "<orig-rfc-id@example.com>"
    assert parsed_msg["References"] == "<orig-rfc-id@example.com>"
    assert "收到，准时参加。" in parsed_msg.get_content()


def test_get_gmail_client_requires_credentials(tmp_path, settings: Settings) -> None:
    # 缺少凭证时明确拒绝，测试替身只能显式注入
    import pytest

    missing = Settings(data_dir=tmp_path, _env_file=None)
    with pytest.raises(RuntimeError, match="未配置"):
        get_gmail_client(missing)

    # 写入伪造凭证文件，验证加载真实客户端
    fake_creds = tmp_path / "credentials.json"
    fake_creds.write_text('{"installed": {}}', encoding="utf-8")

    custom_settings = Settings(
        data_dir=tmp_path,
        gmail_credentials_path=fake_creds,
    )
    custom_client = get_gmail_client(custom_settings)
    assert isinstance(custom_client, GoogleApiGmailClient)
    assert custom_client.credentials_path == fake_creds
