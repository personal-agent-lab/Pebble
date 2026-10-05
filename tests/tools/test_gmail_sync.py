"""触发前按当前标签过滤；跳过与失败均保持同步游标语义。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from server.tools.gmail.sync import GmailSource


def source_with_page(tmp_path, messages):
    client = MagicMock()
    client.list_labels.return_value = [{"id": "Label_42", "name": "News"}]
    client.list_added_messages.return_value = {
        "historyId": "12",
        "history": [{"messagesAdded": [{"message": message} for message in messages]}],
    }
    source = GmailSource(client, path=tmp_path / "sync.json")
    source._save({"email": "owner@example.com", "history_id": "10"})
    accepted = []
    source.agent = SimpleNamespace(accept_new_mail=lambda *args: accepted.append(args))
    return source, client, accepted


def message(mid, labels=None):
    return {"id": mid, "threadId": f"t-{mid}", "labelIds": labels or ["INBOX"]}


def test_news_filtered_using_current_labels_and_cursor_persisted(tmp_path):
    source, client, accepted = source_with_page(
        tmp_path, [message("news"), message("normal"), message("sent", ["SENT"])]
    )
    client.get_message_labels.side_effect = [["INBOX", "Label_42"], ["INBOX", "UNREAD"]]
    asyncio.run(source.poll())
    assert accepted == [("normal", "t-normal")]
    assert json.loads(source.path.read_text())["history_id"] == "12"
    assert client.get_message_labels.call_count == 2
    client.list_labels.assert_called_once()

    # 重启续传，已忽略的来信不回灌。
    restarted = GmailSource(client, path=source.path)
    client.get_profile.return_value = {
        "emailAddress": "owner@example.com",
        "historyId": "12",
    }
    assert asyncio.run(restarted._initialize())
    restarted.agent = source.agent
    client.list_added_messages.return_value = {"historyId": "12", "history": []}
    asyncio.run(restarted.poll())
    client.list_added_messages.assert_called_with("12", None)
    assert accepted == [("normal", "t-normal")]


def test_only_news_skipped_still_advances_cursor(tmp_path):
    source, client, accepted = source_with_page(tmp_path, [message("news")])
    client.get_message_labels.return_value = ["INBOX", "Label_42"]
    asyncio.run(source.poll())
    assert accepted == []
    assert source.state["history_id"] == "12"


def test_deleted_message_does_not_invalidate_history_cursor(tmp_path):
    source, client, accepted = source_with_page(tmp_path, [message("deleted")])
    client.get_message_labels.side_effect = HttpError(
        SimpleNamespace(status=404, reason="gone"), b""
    )
    asyncio.run(source.poll())
    assert accepted == []
    assert source.state["history_id"] == "12"


def test_news_name_exact_match_and_mapping_refreshed(tmp_path):
    source, client, accepted = source_with_page(tmp_path, [message("mail")])
    client.list_labels.return_value = [{"id": "Label_42", "name": "news"}]
    asyncio.run(source.poll())
    assert accepted == [("mail", "t-mail")]
    client.get_message_labels.assert_not_called()

    accepted.clear()
    client.list_labels.return_value = [{"id": "Label_99", "name": "News"}]
    client.get_message_labels.return_value = ["INBOX", "Label_99"]
    asyncio.run(source.poll())
    assert accepted == []
    assert client.list_labels.call_count == 2


@pytest.mark.parametrize("failure", ["list_labels", "get_message_labels"])
def test_label_lookup_failure_does_not_accept_or_advance(tmp_path, failure):
    source, client, accepted = source_with_page(tmp_path, [message("news")])
    getattr(client, failure).side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        asyncio.run(source.poll())
    assert accepted == []
    assert json.loads(source.path.read_text())["history_id"] == "10"


def test_current_labels_override_history_and_mapping_reused_across_pages(tmp_path):
    source, client, accepted = source_with_page(tmp_path, [])
    client.list_added_messages.side_effect = [
        {
            "nextPageToken": "next",
            "history": [{"messagesAdded": [{"message": message("archived")}]}],
        },
        {
            "historyId": "12",
            "history": [
                {
                    "messagesAdded": [
                        {"message": message("normal", ["INBOX", "Label_42"])}
                    ]
                }
            ],
        },
    ]
    client.get_message_labels.side_effect = [["Label_42"], ["INBOX"]]
    asyncio.run(source.poll())
    assert accepted == [("normal", "t-normal")]
    client.list_labels.assert_called_once()
    assert source.state["history_id"] == "12"
