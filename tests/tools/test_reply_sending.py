"""确认后的真实发送与结果核实：明确成功、明确失败与待核实的分界。"""

import hashlib

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from server.tools.gmail.sender import send_message, verify_message
from tests.support.gmail_double import MockGmailClient

FIELDS = dict(
    operation_id="op1",
    version=1,
    kind="reply",
    source_message_id="msg_invite_001",
    thread_id="thread_invite_001",
    to=["alice@example.com"],
    subject="Re: 邀请",
    body="确认内容\n保留空格  ",
    attachments=[],
)

EVIDENCE = {
    key: FIELDS[key]
    for key in (
        "operation_id",
        "kind",
        "source_message_id",
        "thread_id",
        "to",
        "subject",
        "body",
        "attachments",
    )
}


def test_mime_and_exact_confirmed_content():
    client = MockGmailClient()
    assert send_message(**FIELDS, client=client)["status"] == "sent"
    sent = client.sent_log[0]
    assert {key: sent[key] for key in ("to", "subject", "body")} == {
        key: FIELDS[key] for key in ("to", "subject", "body")
    }
    assert sent["in_reply_to"] == "<invite-001@example.com>"
    assert verify_message(**EVIDENCE, client=client)["status"] == "sent"


def test_new_email_uses_same_send_and_verification_contract():
    client = MockGmailClient()
    fields = {
        "operation_id": "new-op",
        "version": 1,
        "kind": "new",
        "to": ["professor@example.edu"],
        "subject": "咨询见面时间",
        "body": "老师您好，请问周五是否方便？",
        "attachments": [],
    }
    assert send_message(**fields, client=client)["status"] == "sent"
    assert client.sent_log[0]["in_reply_to"] is None
    evidence = {key: value for key, value in fields.items() if key != "version"}
    assert verify_message(**evidence, client=client)["status"] == "sent"


def test_attachment_bytes_are_sent_and_verified_by_hash():
    client = MockGmailClient()
    data = b"immutable attachment bytes"
    attachment = {
        "file_id": "file-1",
        "filename": "proof.pdf",
        "mime_type": "application/pdf",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
    }
    fields = {**FIELDS, "operation_id": "attachment-op", "attachments": [attachment]}
    assert send_message(**fields, client=client)["status"] == "sent"
    assert client.sent_log[0]["attachments"] == [
        {key: attachment[key] for key in ("filename", "mime_type", "size", "sha256")}
    ]
    evidence = {key: value for key, value in fields.items() if key != "version"}
    assert verify_message(**evidence, client=client)["status"] == "sent"

    sent_id = client.sent_log[0]["id"]
    attachment_id = client.messages[sent_id].attachments[0].attachment_id
    client.attachments[(sent_id, attachment_id)] = b"tampered"
    assert verify_message(**evidence, client=client)["status"] == "unknown"


@pytest.mark.parametrize("delivered", [True, False])
def test_timeout_only_verifies_no_retry(delivered):
    client = MockGmailClient()
    original = client.send_raw_message
    calls = []

    def timeout(raw, thread_id):
        calls.append(raw)
        if delivered:
            original(raw, thread_id)
        raise TimeoutError("secret must not leak")

    client.send_raw_message = timeout
    result = send_message(**FIELDS, client=client)
    assert result["status"] == ("sent" if delivered else "unknown")
    assert len(calls) == 1
    assert "secret" not in str(result)


def test_old_subject_match_is_not_proof():
    client = MockGmailClient()
    client.raw_send_message(FIELDS["to"], FIELDS["subject"], FIELDS["body"], FIELDS["thread_id"])
    assert verify_message(**EVIDENCE, client=client)["status"] == "unknown"


def test_wrong_thread_and_header_injection_never_send():
    client = MockGmailClient()
    for change in ({"thread_id": "wrong"}, {"subject": "test\nBcc: other@example.com"}):
        assert send_message(**(FIELDS | change), client=client)["status"] == "failed"
    assert not client.sent_log


@pytest.mark.parametrize(
    "http_status,expected", [(401, "failed"), (403, "failed"), (408, "unknown"), (503, "unknown")]
)
def test_http_failure_classification(http_status, expected):
    client = MockGmailClient()

    def reject(raw, thread_id):
        raise HttpError(Response({"status": str(http_status)}), b'{"error":"private"}')

    client.send_raw_message = reject
    result = send_message(**FIELDS, client=client)
    assert result["status"] == expected
    assert "private" not in str(result)
    assert not client.sent_log
