"""tests/test_gmail_validator.py: 测试 Gmail 回复草稿业务校验纯函数。"""

from server.tools.gmail.service import is_valid_email_address, validate_reply_draft


def test_validate_reply_draft_valid_inputs() -> None:
    res = validate_reply_draft(
        source_message_id="msg_123",
        thread_id="thread_456",
        to=["recipient@example.com"],
        subject="Re: 架构方案讨论",
        body="方案已审阅，整体结构清晰，同意推进。",
    )

    assert res["valid"] is True
    assert res["errors"] == []


def test_validate_reply_draft_valid_multiple_and_rfc_addresses() -> None:
    res = validate_reply_draft(
        source_message_id="msg_123",
        thread_id="thread_456",
        to=["Alice <alice@example.com>", "bob.smith+tag@work-domain.co.uk"],
        subject="Re: 进度汇报",
        body="已收到汇报，辛苦了。",
    )

    assert res["valid"] is True
    assert res["errors"] == []


def test_validate_reply_draft_valid_comma_separated_string() -> None:
    res = validate_reply_draft(
        source_message_id="msg_123",
        thread_id="thread_456",
        to="user1@example.com, user2@example.com",
        subject="Re: 测试",
        body="正文",
    )

    assert res["valid"] is True
    assert res["errors"] == []


def test_validate_reply_draft_missing_source_or_thread_id() -> None:
    # 缺失测试
    res = validate_reply_draft(
        source_message_id="   ",
        thread_id="",
        to=["recipient@example.com"],
        subject="Re: 测试",
        body="正文",
    )

    assert res["valid"] is False
    fields = [e["field"] for e in res["errors"]]
    assert "source_message_id" in fields
    assert "thread_id" in fields

    # 包含空白非法字符测试
    res_invalid_chars = validate_reply_draft(
        source_message_id="msg id with spaces",
        thread_id="thread\tid",
        to=["recipient@example.com"],
        subject="Re: 测试",
        body="正文",
    )
    assert res_invalid_chars["valid"] is False
    err_msgs = [e["message"] for e in res_invalid_chars["errors"]]
    assert any("不合法" in m for m in err_msgs)


def test_validate_reply_draft_empty_or_invalid_recipient() -> None:
    # 1. 空收件人
    res_empty = validate_reply_draft(
        source_message_id="msg_123",
        thread_id="thread_456",
        to=[],
        subject="Re: 测试",
        body="正文",
    )
    assert res_empty["valid"] is False
    assert any(e["field"] == "to" for e in res_empty["errors"])

    # 2. 格式非法的收件人
    res_malformed = validate_reply_draft(
        source_message_id="msg_123",
        thread_id="thread_456",
        to=["valid@example.com", "not-an-email", "@missing-user.com"],
        subject="Re: 测试",
        body="正文",
    )
    assert res_malformed["valid"] is False
    to_errors = [e for e in res_malformed["errors"] if e["field"] == "to"]
    assert len(to_errors) == 2


def test_validate_reply_draft_empty_subject_and_body() -> None:
    res = validate_reply_draft(
        source_message_id="msg_123",
        thread_id="thread_456",
        to=["recipient@example.com"],
        subject="   ",
        body="\n\t  ",
    )

    assert res["valid"] is False
    fields = [e["field"] for e in res["errors"]]
    assert "subject" in fields
    assert "body" in fields


def test_validate_reply_draft_is_pure_function() -> None:
    args = {
        "source_message_id": "msg_123",
        "thread_id": "thread_456",
        "to": ["user@example.com"],
        "subject": "主题",
        "body": "正文",
    }
    res1 = validate_reply_draft(**args)
    res2 = validate_reply_draft(**args)
    assert res1 == res2
    assert res1["valid"] is True


def test_is_valid_email_address_edge_cases() -> None:
    assert is_valid_email_address("user@domain.com") is True
    assert is_valid_email_address("John Doe <john.doe@company.org>") is True
    assert is_valid_email_address("invalid-email") is False
    assert is_valid_email_address("user@") is False
    assert is_valid_email_address("@domain.com") is False
    assert is_valid_email_address("") is False
