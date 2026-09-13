"""手动验收用的替身装配走一遍七步流程：真实 HTTP、SQLite 与确认执行，邮件与 Agent 为替身。

这条测试保证模拟邮箱、会话式 Agent 替身和 `tests.support.manual_backend` 不会悄悄坏掉；
浏览器手动验收依赖同一套装配。
"""

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import Settings

MAILS = Path(__file__).resolve().parents[1] / "support" / "mails"

FINAL_BODY = "尊敬的先生／女士：\n\n我确认参加周五下午 3 点的交流会，请发我会议链接。\n\n顺颂商祺。"


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PEBBLE_TEST_INBOX_DIR", str(settings.data_dir / "inbox"))
    monkeypatch.setenv("PEBBLE_TEST_AGENT_DELAY", "0")
    from tests.support.manual_backend import build_app

    app = build_app()
    app.state.mail_source.interval = 0.02
    with TestClient(app) as started:
        yield started


def deliver(settings: Settings, name: str) -> None:
    inbox = settings.data_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MAILS / f"{name}.json", inbox / f"{name}.json")


def wait_for(read, description, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"等待超时：{description}")


def settled(client: TestClient, task_id: str) -> dict | None:
    task = client.get(f"/api/tasks/{task_id}").json()
    run = task["latest_run"]
    return task if run is not None and run["status"] in ("done", "error") else None


def say(client: TestClient, task_id: str, message: str) -> dict:
    accepted = client.post(f"/api/tasks/{task_id}/messages", json={"message": message})
    assert accepted.status_code == 202, accepted.text
    task = wait_for(lambda: settled(client, task_id), f"消息处理完成：{message}")
    assert task["latest_run"]["status"] == "done", task["latest_run"]["error"]
    return task


def operations(client: TestClient, task_id: str) -> list[dict]:
    return client.get(f"/api/tasks/{task_id}/operations").json()


def test_seven_steps(client: TestClient, settings: Settings) -> None:
    # 1. 后台检测到新邮件并建任务；同一封邮件重复投递不重复建。
    deliver(settings, "invite")
    tasks = wait_for(lambda: client.get("/api/tasks").json(), "新邮件任务出现")
    task_id = tasks[0]["task_id"]
    deliver(settings, "invite")
    time.sleep(0.1)
    assert len(client.get("/api/tasks").json()) == 1

    # 2. Agent 给出摘要和建议，且没有自作主张建草稿。
    task = wait_for(lambda: settled(client, task_id), "新邮件分析完成")
    assert task["sdk_session_id"] is not None
    history = client.get(f"/api/tasks/{task_id}/history").json()["messages"]
    assert "周五项目交流会邀请" in history[0]["text"]
    assert operations(client, task_id) == []

    # 3. 普通提问不等于要草稿。
    say(client, task_id, "这封邮件说了什么？")
    assert operations(client, task_id) == []

    # 4. 要求准备回信才生成草稿。
    say(client, task_id, "帮我写一封回信")
    [operation] = operations(client, task_id)
    assert operation["version"] == 1 and operation["status"] == "pending"
    draft = client.get(f"/api/operations/{operation['operation_id']}/draft").json()
    assert draft["to"] == ["organizer@example.com"]
    assert draft["subject"] == "回复：周五项目交流会邀请"

    # 5. 多轮修改与直接编辑各产生一版。
    say(client, task_id, "再问一下会议链接")
    say(client, task_id, "语气正式一点")
    edited = client.patch(
        f"/api/operations/{operation['operation_id']}/draft",
        json={
            "expected_version": 3,
            "to": draft["to"],
            "subject": draft["subject"],
            "body": FINAL_BODY,
        },
    ).json()
    assert edited["version"] == 4

    # 旧版本确认被拒，且不产生发送调用。
    stale = client.post(
        f"/api/tasks/{task_id}/confirmations",
        json={"operation_id": operation["operation_id"], "version": 3},
    )
    assert stale.status_code == 409
    assert not (settings.data_dir / "sent.jsonl").exists()

    # 6. 确认当前版本后真正调用发送替身。
    confirmed = client.post(
        f"/api/tasks/{task_id}/confirmations",
        json={"operation_id": operation["operation_id"], "version": 4},
    )
    assert confirmed.status_code == 202
    execution = wait_for(
        lambda: (
            view
            if (view := client.get(f"/api/operations/{operation['operation_id']}/execution").json())
            and view["status"] == "sent"
            else None
        ),
        "发送完成",
    )
    assert execution["result"]["message_id"] == "test-message"
    sent = [
        json.loads(line) for line in (settings.data_dir / "sent.jsonl").read_text().splitlines()
    ]
    assert len(sent) == 1
    current = client.get(f"/api/operations/{operation['operation_id']}/draft").json()
    assert sent[0]["body"] == current["body"] and sent[0]["to"] == current["to"]

    # 7. 结果回传后 Agent 在同一会话里给出后续回复。
    wait_for(
        lambda: any(
            "已经发出去了" in message["text"]
            for message in client.get(f"/api/tasks/{task_id}/history").json()["messages"]
        ),
        "结果回传后的回复",
    )


def test_second_mail_gets_its_own_task_and_draft(client: TestClient, settings: Settings) -> None:
    deliver(settings, "invite")
    deliver(settings, "reschedule")
    tasks = wait_for(
        lambda: rows if len(rows := client.get("/api/tasks").json()) == 2 else None,
        "两封邮件两个任务",
    )

    drafts = []
    for task in tasks:
        wait_for(lambda task_id=task["task_id"]: settled(client, task_id), "分析完成")
        say(client, task["task_id"], "帮我写一封回信")
        [operation] = operations(client, task["task_id"])
        drafts.append(client.get(f"/api/operations/{operation['operation_id']}/draft").json())

    assert {draft["source_message_id"] for draft in drafts} == {
        "mail-invite-001",
        "mail-reschedule-002",
    }
    assert drafts[0]["operation_id"] != drafts[1]["operation_id"]


def build(settings: Settings):
    from tests.support.manual_backend import build_app

    app = build_app()
    app.state.mail_source.interval = 0.02
    return app


def test_restart_keeps_each_task_history_apart(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启后新任务不能捡到上一轮任务的会话历史。"""
    monkeypatch.setenv("PEBBLE_TEST_INBOX_DIR", str(settings.data_dir / "inbox"))
    monkeypatch.setenv("PEBBLE_TEST_AGENT_DELAY", "0")

    with TestClient(build(settings)) as client:
        deliver(settings, "invite")
        first = wait_for(lambda: client.get("/api/tasks").json(), "第一封邮件建任务")[0]["task_id"]
        wait_for(lambda: settled(client, first), "第一封分析完成")

    with TestClient(build(settings)) as client:
        deliver(settings, "reschedule")
        tasks = wait_for(
            lambda: rows if len(rows := client.get("/api/tasks").json()) == 2 else None,
            "第二封邮件建任务",
        )
        second = next(task["task_id"] for task in tasks if task["task_id"] != first)
        wait_for(lambda: settled(client, second), "第二封分析完成")

        fresh = client.get(f"/api/tasks/{second}/history").json()["messages"]
        assert fresh and all("周五项目交流会邀请" not in message["text"] for message in fresh)
        kept = client.get(f"/api/tasks/{first}/history").json()["messages"]
        assert any("周五项目交流会邀请" in message["text"] for message in kept)


def test_composed_mail_without_summary(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """现写的邮件不带摘要字段，替身按正文首句摘要，并且能据此起草回信。"""
    monkeypatch.setenv("PEBBLE_TEST_INBOX_DIR", str(settings.data_dir / "inbox"))
    monkeypatch.setenv("PEBBLE_TEST_AGENT_DELAY", "0")
    from tests.mail_inbox import main

    assert (
        main(
            [
                "compose",
                "--from",
                "lawyer@example.com",
                "--subject",
                "合同条款确认",
                "--body",
                "你好，\\n\\n第 7 条的付款周期希望改成 30 天，能接受吗？",
                "--id",
                "mail-compose-1",
            ]
        )
        == 0
    )

    with TestClient(build(settings)) as client:
        task_id = wait_for(lambda: client.get("/api/tasks").json(), "现写的邮件建任务")[0][
            "task_id"
        ]
        wait_for(lambda: settled(client, task_id), "分析完成")
        messages = client.get(f"/api/tasks/{task_id}/history").json()["messages"]
        assert "第 7 条的付款周期" in messages[0]["text"]
        assert "你好，" not in messages[0]["text"].split("摘要：")[1]

        say(client, task_id, "帮我写一封回信")
        [operation] = operations(client, task_id)
        draft = client.get(f"/api/operations/{operation['operation_id']}/draft").json()
        assert draft["to"] == ["lawyer@example.com"]
        assert draft["subject"] == "回复：合同条款确认"
        assert draft["source_message_id"] == "mail-compose-1"
