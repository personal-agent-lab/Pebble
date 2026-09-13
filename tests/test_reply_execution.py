import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from qoder_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock

from server.agent import sdk_client
from server.tools.gmail.client import MockGmailClient
from server.tools.gmail.protocol import InMemoryDraftStorage
from server.tools.gmail.sender import send_reply, verify_reply_status
from server.tools.gmail.tools import prepare_reply

FIELDS = dict(
    operation_id="op1",
    version=1,
    source_message_id="msg_invite_001",
    thread_id="thread_invite_001",
    to=["alice@example.com"],
    subject="Re: 邀请",
    body="确认内容\n保留空格  ",
)


def test_mime_and_exact_confirmed_content():
    client = MockGmailClient()
    assert send_reply(**FIELDS, client=client)["status"] == "sent"
    sent = client.sent_log[0]
    assert {key: sent[key] for key in ("to", "subject", "body")} == {
        key: FIELDS[key] for key in ("to", "subject", "body")
    }
    assert sent["in_reply_to"] == "<invite-001@example.com>"
    assert (
        verify_reply_status(FIELDS["thread_id"], FIELDS["source_message_id"], client=client)[
            "status"
        ]
        == "sent"
    )


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
    result = send_reply(**FIELDS, client=client)
    assert result["status"] == ("sent" if delivered else "unknown")
    assert len(calls) == 1
    assert "secret" not in str(result)


def test_old_subject_match_is_not_proof():
    client = MockGmailClient()
    client.raw_send_reply(FIELDS["to"], FIELDS["subject"], FIELDS["body"], FIELDS["thread_id"])
    assert (
        verify_reply_status(FIELDS["thread_id"], FIELDS["source_message_id"], client=client)[
            "status"
        ]
        == "unknown"
    )


def test_wrong_thread_recipient_and_header_injection_never_send():
    client = MockGmailClient()
    for change in (
        {"thread_id": "wrong"},
        {"to": ["other@example.com"]},
        {"subject": "test\nBcc: other@example.com"},
    ):
        assert send_reply(**(FIELDS | change), client=client)["status"] == "failed"
    assert not client.sent_log


def test_parallel_drafts_and_defensive_copy():
    storage = InMemoryDraftStorage()
    data = {key: value for key, value in FIELDS.items() if key not in {"operation_id", "version"}}
    data["task_id"] = "task1"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: storage.save_reply_draft(**data), range(50)))
    assert len({result["operation_id"] for result in results}) == 1
    draft = storage.get_draft(results[0]["operation_id"])
    draft["to"].append("intruder@example.com")
    assert storage.get_draft(results[0]["operation_id"])["to"] == FIELDS["to"]


def test_schema_hides_storage_and_types_recipients():
    schema = prepare_reply.parameters_schema
    assert "storage" not in schema["properties"]
    assert schema["properties"]["to"] == {"type": "array", "items": {"type": "string"}}


async def collect(stream):
    return [event async for event in stream]


def install_sdk_stub(monkeypatch, messages):
    captured = []

    class Client:
        def __init__(self, options):
            captured.append(options)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, message):
            self.message = message

        async def receive_response(self):
            for message in messages:
                yield message

    monkeypatch.setattr(sdk_client, "QoderSDKClient", Client)
    return captured


def result(error=False):
    return ResultMessage("success", 1, 1, error, 1, "session1")


def test_stream_resume_delta_and_no_duplicate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sdk_client, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path, qoder_model=None)
    )
    captured = install_sdk_stub(
        monkeypatch,
        [
            SystemMessage("init", {"session_id": "session1"}),
            StreamEvent(
                "e",
                "session1",
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "草稿"}},
            ),
            AssistantMessage([TextBlock("草稿")], "model"),
            result(),
        ],
    )
    events = asyncio.run(collect(sdk_client.stream_agent_turn("task", "回复", "session1")))
    assert [event["type"] for event in events] == ["session", "text", "done"]
    assert captured[0].resume == "session1"
    assert captured[0].tools == []
    assert not any("send_reply" in tool for tool in captured[0].allowed_tools)
    assert captured[0].setting_sources == []


@pytest.mark.parametrize("ending", [[], [result(True)]])
def test_stream_abnormal_end(monkeypatch, tmp_path, ending):
    monkeypatch.setattr(
        sdk_client, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path, qoder_model=None)
    )
    install_sdk_stub(monkeypatch, [SystemMessage("init", {"session_id": "session1"}), *ending])
    events = asyncio.run(collect(sdk_client.stream_agent_turn("task", "回复")))
    assert [event["type"] for event in events] == ["session", "error"]


@pytest.mark.parametrize("status", ["sent", "failed", "unknown"])
def test_result_returns_to_original_session(monkeypatch, tmp_path, status):
    monkeypatch.setattr(
        sdk_client, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path, qoder_model=None)
    )
    captured = install_sdk_stub(
        monkeypatch, [SystemMessage("init", {"session_id": "session1"}), result()]
    )
    events = asyncio.run(
        collect(
            sdk_client.feed_execution_result(
                "task", "session1", "op1", 2, {"status": status, "message_id": "sent1"}
            )
        )
    )
    assert captured[0].resume == "session1"
    assert f'"status": "{status}"' in captured[0].system_prompt
    assert events[-1] == {"type": "done"}


@pytest.mark.parametrize(
    "http_status,expected", [(401, "failed"), (403, "failed"), (408, "unknown"), (503, "unknown")]
)
def test_http_failure_classification(http_status, expected):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    client = MockGmailClient()

    def reject(raw, thread_id):
        raise HttpError(Response({"status": str(http_status)}), b'{"error":"private"}')

    client.send_raw_message = reject
    result = send_reply(**FIELDS, client=client)
    assert result["status"] == expected
    assert "private" not in str(result)
    assert not client.sent_log
