"""tests/test_gmail_validator.py: 测试 Gmail 邮件草稿业务校验纯函数。"""

from server.tools.gmail.service import is_valid_email_address, validate_mail_draft


def test_validate_mail_draft_valid_inputs() -> None:
    res = validate_mail_draft(
        kind="reply",
        source_message_id="msg_123",
        thread_id="thread_456",
        to=["recipient@example.com"],
        subject="Re: 架构方案讨论",
        body="方案已审阅，整体结构清晰，同意推进。",
    )

    assert res["valid"] is True
    assert res["errors"] == []


def test_validate_mail_draft_valid_multiple_and_rfc_addresses() -> None:
    res = validate_mail_draft(
        kind="reply",
        source_message_id="msg_123",
        thread_id="thread_456",
        to=["Alice <alice@example.com>", "bob.smith+tag@work-domain.co.uk"],
        subject="Re: 进度汇报",
        body="已收到汇报，辛苦了。",
    )

    assert res["valid"] is True
    assert res["errors"] == []


def test_validate_mail_draft_rejects_string_recipients() -> None:
    res = validate_mail_draft(
        kind="reply",
        source_message_id="msg_123",
        thread_id="thread_456",
        to="user1@example.com, user2@example.com",
        subject="Re: 测试",
        body="正文",
    )

    assert res["valid"] is False
    assert [error["field"] for error in res["errors"]] == ["to"]


def test_validate_mail_draft_missing_source_or_thread_id() -> None:
    # 缺失测试
    res = validate_mail_draft(
        kind="reply",
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
    res_invalid_chars = validate_mail_draft(
        kind="reply",
        source_message_id="msg id with spaces",
        thread_id="thread\tid",
        to=["recipient@example.com"],
        subject="Re: 测试",
        body="正文",
    )
    assert res_invalid_chars["valid"] is False
    err_msgs = [e["message"] for e in res_invalid_chars["errors"]]
    assert any("不合法" in m for m in err_msgs)


def test_validate_mail_draft_allows_empty_recipients_until_sending() -> None:
    draft = {
        "kind": "reply",
        "source_message_id": "msg_123",
        "thread_id": "thread_456",
        "subject": "Re: 测试",
        "body": "正文",
    }

    # 1. 草稿只要求主题与正文：收件人可以空着，等用户在卡片上补填
    res_empty = validate_mail_draft(to=[], **draft)
    assert res_empty == {"valid": True, "errors": []}

    # 2. 发送口径额外要求至少一个收件人
    res_sending = validate_mail_draft(to=[], require_recipients=True, **draft)
    assert res_sending["valid"] is False
    assert [e["field"] for e in res_sending["errors"]] == ["to"]


def test_validate_mail_draft_rejects_malformed_recipient() -> None:
    res_malformed = validate_mail_draft(
        kind="reply",
        source_message_id="msg_123",
        thread_id="thread_456",
        to=["valid@example.com", "not-an-email", "@missing-user.com"],
        subject="Re: 测试",
        body="正文",
    )
    assert res_malformed["valid"] is False
    to_errors = [e for e in res_malformed["errors"] if e["field"] == "to"]
    assert len(to_errors) == 2


def test_validate_mail_draft_empty_subject_and_body() -> None:
    res = validate_mail_draft(
        kind="reply",
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


def test_validate_mail_draft_is_pure_function() -> None:
    args = {
        "kind": "reply",
        "source_message_id": "msg_123",
        "thread_id": "thread_456",
        "to": ["user@example.com"],
        "subject": "主题",
        "body": "正文",
    }
    res1 = validate_mail_draft(**args)
    res2 = validate_mail_draft(**args)
    assert res1 == res2
    assert res1["valid"] is True


def test_is_valid_email_address_edge_cases() -> None:
    assert is_valid_email_address("user@domain.com") is True
    assert is_valid_email_address("John Doe <john.doe@company.org>") is True
    assert is_valid_email_address("invalid-email") is False
    assert is_valid_email_address("user@") is False
    assert is_valid_email_address("@domain.com") is False
    assert is_valid_email_address("") is False
