"""评测基础设施回归；不对场景结果作程序断言。"""

import base64
from email.message import EmailMessage

import pytest

from scenario_eval.run import Runner, visible
from scenario_eval.world import Calendar, Clock, Mailbox
from server.tools.gmail.sender import send_message, verify_message


def clock(tmp_path):
    value = Clock(tmp_path / "clock.json")
    value.set("2026-10-04T10:00:00+08:00")
    return value


def test_virtual_send_response_lost_can_be_verified_without_resending(tmp_path):
    mail = Mailbox(tmp_path, "user@example.test", clock(tmp_path), {"send_response_lost": True})
    source = mail.deliver(
        {
            "id": "incoming",
            "thread_id": "thread",
            "rfc_message_id": "<source@example.test>",
            "from_addr": "peer@example.test",
            "to_addrs": ["user@example.test"],
            "subject": "原始主题",
            "body_text": "原始正文",
        }
    )
    fields = dict(
        operation_id="operation",
        kind="reply",
        to=["peer@example.test"],
        subject="Re: 原始主题",
        body="保持原文\n第二行",
        source_message_id=source.id,
        thread_id=source.thread_id,
        client=mail,
    )
    sent = send_message(version=1, **fields)
    assert sent["status"] == "sent"
    assert verify_message(**fields)["status"] == "sent"
    assert len(mail.received["peer@example.test"]) == 1
    delivered = mail.received["peer@example.test"][0]
    assert delivered["body_text"] == fields["body"]
    assert delivered["in_reply_to"] == source.rfc_message_id
    assert len(mail.search_messages("in:sent")) == 1


def test_virtual_mime_threads_and_incremental_polling(tmp_path):
    mail = Mailbox(tmp_path, "user@example.test", clock(tmp_path))
    before = mail.get_profile()["historyId"]
    mail.deliver(
        {
            "id": "new",
            "thread_id": "thread",
            "rfc_message_id": "<new@example.test>",
            "from_addr": "peer@example.test",
            "to_addrs": ["user@example.test"],
            "subject": "test",
            "body_text": "seed",
        }
    )
    assert (
        mail.list_added_messages(before)["history"][0]["messagesAdded"][0]["message"]["id"] == "new"
    )
    assert mail.list_added_messages(mail.get_profile()["historyId"])["history"] == []
    mime = EmailMessage()
    mime["To"], mime["Subject"], mime["Message-ID"] = "peer@example.test", "test", "<sent@test>"
    mime.set_content("hello")
    mail.send_raw_message(base64.urlsafe_b64encode(mime.as_bytes()).decode(), "thread")
    assert len(mail.get_thread("thread")) == 2


def test_calendar_overlap_all_day_and_response_loss(tmp_path):
    calendar = Calendar(tmp_path, [], clock(tmp_path), {"calendar_response_lost": True})
    fields = {"summary": "休假", "start": "2026-10-05", "end": "2026-10-06", "all_day": True}
    assert (
        calendar.create_event(operation_id="one", version=1, fields=fields)["status"] == "unknown"
    )
    assert calendar.verify_event(operation_id="one", fields=fields)["status"] == "created"
    assert (
        len(
            calendar.check_conflicts("2026-10-05T10:00:00+08:00", "2026-10-05T11:00:00+08:00")[
                "conflicts"
            ]
        )
        == 1
    )
    assert (
        calendar.check_conflicts("2026-10-06T08:00:00+08:00", "2026-10-06T09:00:00+08:00")[
            "conflicts"
        ]
        == []
    )


def test_simulator_cannot_see_internal_observations():
    items = [
        {"kind": "text", "text": "hello"},
        {"kind": "tool", "result": "secret"},
        {"kind": "mail_draft", "draft": {"body": "draft"}},
    ]
    assert [i["kind"] for i in visible(items)] == ["text", "mail_draft"]


def test_clock_wait_and_unknown_task_are_runtime_constraints(tmp_path):
    clock(tmp_path)
    runner = Runner(None, tmp_path, "model")
    assert runner.execute({"action": "wait", "until": "2026-10-05T09:00:00+08:00"})
    assert runner.clock.now().day == 5
    with pytest.raises(ValueError, match="未来时间"):
        runner.execute({"action": "wait", "until": "2026-10-04T09:00:00+08:00"})
    with pytest.raises(ValueError, match="非本次"):
        runner.execute({"action": "say", "task_id": "other", "message": "hi"})


def test_model_names_resolve_live_ids_without_fallback():
    from scenario_eval.models import select_model

    catalog = [
        {"value": "custom-id", "displayName": "DeepSeek-Flash", "isEnabled": True},
        {"value": "qmodel_38max", "displayName": "Qwen3.8-Max", "isEnabled": True},
    ]
    assert select_model("deepseek-flash", catalog) == "custom-id"
    assert select_model("qwen3.8-max", catalog) == "qmodel_38max"
    with pytest.raises(ValueError):
        select_model("missing", catalog)


def test_managed_model_preferred_when_custom_label_duplicates():
    from scenario_eval.models import select_model

    catalog = [
        {"value": "dfmodel", "displayName": "DeepSeek-Flash", "source": "system"},
        {"value": "custom-id", "displayName": "DeepSeek-Flash", "source": "user"},
    ]
    assert select_model("deepseek-flash", catalog) == "dfmodel"
    assert select_model("custom-id", catalog) == "custom-id"


def test_second_confirmation_does_not_reuse_previous_execution_turn(tmp_path, monkeypatch):
    from types import SimpleNamespace

    clock(tmp_path)
    old = {"run_id": "previous", "kind": "execution_result", "status": "done"}
    new = {"run_id": "current", "kind": "execution_result", "status": "done"}
    runs = iter([old, old, new])
    api = SimpleNamespace(
        get_draft=lambda op: {"operation_id": op},
        timeline=lambda task: [{"kind": "mail_draft", "draft": {"operation_id": "card"}}],
        get_task=lambda task: {"latest_run": next(runs)},
        confirm=lambda *args: {"status": "sending"},
    )
    runner = Runner(api, tmp_path, "model")
    runner.tasks = ["task"]
    observed = []
    monkeypatch.setattr(runner, "wait_turn", lambda task, run: observed.append(run["run_id"]))
    monkeypatch.setattr("scenario_eval.run.time.sleep", lambda seconds: None)
    runner.execute({"action": "confirm", "task_id": "task", "operation_id": "card", "version": 2})
    assert observed == ["current"]
