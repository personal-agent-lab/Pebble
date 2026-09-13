"""后台调用与回传：真实落盘 SQLite、真实存储与确认服务 + Agent 与发送替身。"""

import asyncio
import json
import threading
import time
from typing import NamedTuple

import pytest

from server.approval.service import ConfirmationService
from server.db import init_db, session, write
from server.errors import NotFoundError
from server.gateway.runtime import INTERRUPTED_REASON, GatewayRuntime
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore
from tests.support.agent_double import FakeAgentGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def drain(service):
    async with asyncio.timeout(5):
        while service._active or service._sends:
            await asyncio.gather(*list(service._active.values()), *list(service._sends.values()))


DRAFT = {
    "to": ["甲@example.com"],
    "subject": "回复：活动邀请",
    "body": "你好，\n\n我参加。\n",
}


def valid(**kwargs):
    return {"valid": True, "errors": []}


class Sender:
    """发送替身：记录每次调用的参数。"""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **fields):
        self.calls.append(fields)
        return {"status": "sent", "message_id": "sent-1"}


async def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


class Flow(NamedTuple):
    service: GatewayRuntime
    gateway: FakeAgentGateway
    confirmations: ConfirmationService
    sender: Sender
    tasks: SessionStore
    drafts: ReplyDraftStore


@pytest.fixture
async def flow(settings):
    init_db()
    gateway = FakeAgentGateway()
    sender = Sender()
    confirmations = ConfirmationService(sender)
    service = GatewayRuntime(gateway, confirmations=confirmations)
    try:
        yield Flow(
            service=service,
            gateway=gateway,
            confirmations=confirmations,
            sender=sender,
            tasks=SessionStore(),
            drafts=ReplyDraftStore(valid),
        )
    finally:
        await service.close()


async def test_new_mail_runs_summary_without_draft_or_send(flow):
    task = flow.service.accept_new_mail("m1", "thread-1")
    assert task["goal"] == "处理新收到的邮件"
    await drain(flow.service)

    assert flow.gateway.calls_of("new_mail") == [
        {
            "kind": "new_mail",
            "task_id": task["task_id"],
            "sdk_session_id": None,
            "source_message_id": "m1",
            "thread_id": "thread-1",
        }
    ]
    assert flow.tasks.get_task(task["task_id"])["sdk_session_id"] == "fake-session-1"
    assert flow.tasks.list_task_operations(task["task_id"]) == []
    assert flow.sender.calls == []

    run = flow.service.latest_run(task["task_id"])
    assert (run["kind"], run["status"], run["error"]) == ("new_mail", "done", None)
    assert run["started_at"] and run["finished_at"]


async def test_same_message_reuses_task_and_new_message_new_task(flow):
    first = flow.service.accept_new_mail("m1", "thread-1")
    duplicate = flow.service.accept_new_mail("m1", "thread-1")
    same_thread = flow.service.accept_new_mail("m2", "thread-1")

    assert duplicate == first
    assert same_thread["task_id"] != first["task_id"]
    await drain(flow.service)
    assert [call["source_message_id"] for call in flow.gateway.calls_of("new_mail")] == ["m1", "m2"]
    assert len(flow.service.list_runs(first["task_id"])) == 1
    assert len(flow.service.list_runs(same_thread["task_id"])) == 1


async def test_user_request_saves_draft_without_sending(flow):
    task = flow.service.accept_new_mail("m1", "thread-1")
    await drain(flow.service)

    def handler(*, task_id, sdk_session_id, message, **kwargs):
        async def events():
            draft = flow.drafts.save_reply_draft(task_id, "m1", "thread-1", **DRAFT)
            yield {
                "type": "draft_saved",
                "operation_id": draft["operation_id"],
                "version": draft["version"],
            }
            yield {"type": "text", "text": "草稿已保存，请确认后发送。"}
            yield {"type": "done"}

        return events()

    flow.gateway.handle("message", handler)
    run = flow.service.submit_message(task["task_id"], "请帮我写回复")
    assert run["kind"] == "message" and run["status"] == "pending"
    await drain(flow.service)

    call = flow.gateway.calls_of("message")[0]
    assert call["sdk_session_id"] == "fake-session-1"
    assert call["message"] == "请帮我写回复"
    operations = flow.tasks.list_task_operations(task["task_id"])
    assert [(op["version"], op["status"]) for op in operations] == [(1, "pending")]
    assert flow.drafts.get_reply_draft(operations[0]["operation_id"])["body"] == DRAFT["body"]
    assert flow.sender.calls == []
    assert flow.service.get_run(run["run_id"])["status"] == "done"


async def test_turn_can_end_without_draft(flow):
    task = flow.tasks.create_task("检查邀请")

    def handler(**kwargs):
        async def events():
            yield {"type": "session", "sdk_session_id": "sdk-simple"}
            yield {"type": "text", "text": "这封邮件不需要回复。"}
            yield {"type": "done"}

        return events()

    flow.gateway.handle("message", handler)
    run = flow.service.submit_message(task["task_id"], "需要回复吗？")
    await drain(flow.service)

    assert flow.service.get_run(run["run_id"])["status"] == "done"
    assert flow.tasks.list_task_operations(task["task_id"]) == []
    assert flow.sender.calls == []
    assert flow.tasks.get_task(task["task_id"])["sdk_session_id"] == "sdk-simple"


async def test_same_task_inputs_run_in_acceptance_order(flow):
    task = flow.tasks.create_task("依次处理")
    release = threading.Event()
    seen: list[str] = []

    def handler(*, message, **kwargs):
        async def events():
            seen.append(message)
            if message == "第一条":
                await asyncio.to_thread(release.wait, 5)
            yield {"type": "text", "text": f"回复：{message}"}
            yield {"type": "done"}

        return events()

    flow.gateway.handle("message", handler)
    flow.service.submit_message(task["task_id"], "第一条")
    assert await wait_for(lambda: seen == ["第一条"])
    flow.service.submit_message(task["task_id"], "第二条")
    await asyncio.sleep(0.05)
    assert seen == ["第一条"]

    release.set()
    await drain(flow.service)
    assert seen == ["第一条", "第二条"]


async def test_different_tasks_do_not_block_each_other(flow):
    first = flow.tasks.create_task("任务一")
    second = flow.tasks.create_task("任务二")
    release = threading.Event()
    completed: list[str] = []

    def handler(*, message, **kwargs):
        async def events():
            if message == "慢":
                await asyncio.to_thread(release.wait, 5)
            completed.append(message)
            yield {"type": "done"}

        return events()

    flow.gateway.handle("message", handler)
    slow = flow.service.submit_message(first["task_id"], "慢")
    await wait_for(lambda: flow.service.get_run(slow["run_id"])["status"] == "running")
    fast = flow.service.submit_message(second["task_id"], "快")

    await wait_for(lambda: flow.service.get_run(fast["run_id"])["status"] == "done")
    assert completed == ["快"]
    release.set()
    await drain(flow.service)
    assert completed == ["快", "慢"]
    assert flow.service.get_run(slow["run_id"])["status"] == "done"


async def test_gateway_failure_marks_run_error_and_next_run_works(flow):
    task = flow.tasks.create_task("容忍失败")

    def failing(**kwargs):
        async def events():
            yield {"type": "text", "text": "开始处理"}
            raise RuntimeError("模拟网关故障")

        return events()

    flow.gateway.handle("message", failing)
    run = flow.service.submit_message(task["task_id"], "会失败")
    await drain(flow.service)
    row = flow.service.get_run(run["run_id"])
    assert row["status"] == "error"
    assert "模拟网关故障" in row["error"]

    def ok(**kwargs):
        async def events():
            yield {"type": "done"}

        return events()

    flow.gateway.handle("message", ok)
    second = flow.service.submit_message(task["task_id"], "之后正常")
    await drain(flow.service)
    assert flow.service.get_run(second["run_id"])["status"] == "done"


async def test_malformed_event_stream_is_protocol_error(flow):
    task = flow.tasks.create_task("协议校验")

    def unfinished(**kwargs):
        async def events():
            yield {"type": "text", "text": "没有结束事件"}

        return events()

    flow.gateway.handle("message", unfinished)
    run = flow.service.submit_message(task["task_id"], "无结束")
    await drain(flow.service)
    row = flow.service.get_run(run["run_id"])
    assert row["status"] == "error"
    assert "done" in row["error"] and "error" in row["error"]

    def broken(**kwargs):
        async def events():
            yield {"type": "draft_saved", "operation_id": "o1"}

        return events()

    flow.gateway.handle("message", broken)
    second = flow.service.submit_message(task["task_id"], "坏事件")
    await drain(flow.service)
    assert "draft_saved" in flow.service.get_run(second["run_id"])["error"]


async def test_events_are_published_with_run_id(flow):
    release = threading.Event()

    def handler(**kwargs):
        async def events():
            await asyncio.to_thread(release.wait, 5)
            yield {"type": "session", "sdk_session_id": "sdk-events"}
            yield {"type": "text", "text": "新邮件摘要与建议"}
            yield {"type": "done"}

        return events()

    flow.gateway.handle("new_mail", handler)
    task = flow.service.accept_new_mail("m1", "thread-1")
    subscription = flow.service.events.subscribe(task["task_id"])
    release.set()

    events = [await asyncio.wait_for(subscription.get(), 5) for _ in range(3)]
    assert [event["type"] for event in events] == ["session", "text", "done"]
    run_id = flow.service.list_runs(task["task_id"])[0]["run_id"]
    assert {event["run_id"] for event in events} == {run_id}
    assert events[1]["text"] == "新邮件摘要与建议"
    assert flow.service.events.subscriber_count(task["task_id"]) == 1


async def test_history_reads_the_bound_session(flow):
    task = flow.service.accept_new_mail("m1", "thread-1")
    await drain(flow.service)
    flow.service.submit_message(task["task_id"], "请帮我写回复")
    await drain(flow.service)

    history = await flow.service.read_history(task["task_id"])
    assert history["sdk_session_id"] == "fake-session-1"
    assert history["messages"] == [
        {"role": "assistant", "text": "新邮件 m1 的摘要与建议"},
        {"role": "user", "text": "请帮我写回复"},
        {"role": "assistant", "text": "收到：请帮我写回复"},
    ]

    empty = flow.tasks.create_task("尚无会话")
    assert await flow.service.read_history(empty["task_id"]) == {
        "task_id": empty["task_id"],
        "sdk_session_id": None,
        "messages": [],
    }


async def test_message_for_missing_task_is_not_found(flow):
    with pytest.raises(NotFoundError):
        flow.service.submit_message("missing-task", "你好")
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


async def test_resume_interrupts_running_and_executes_pending(flow):
    task = flow.tasks.create_task("重启恢复")
    with session() as conn, write(conn):
        conn.execute(
            "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at, started_at) "
            "VALUES (?, ?, 'message', ?, 'running', ?, ?)",
            (
                "run-old",
                task["task_id"],
                json.dumps({"message": "旧"}),
                "2026-09-12T00:00:00+00:00",
                "2026-09-12T00:00:01+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at) "
            "VALUES (?, ?, 'message', ?, 'pending', ?)",
            (
                "run-new",
                task["task_id"],
                json.dumps({"message": "新"}),
                "2026-09-12T00:00:02+00:00",
            ),
        )

    assert flow.service.resume() == ["run-old"]
    await drain(flow.service)
    old = flow.service.get_run("run-old")
    assert old["status"] == "interrupted"
    assert old["error"] == INTERRUPTED_REASON
    assert flow.service.get_run("run-new")["status"] == "done"
    assert [call["message"] for call in flow.gateway.calls_of("message")] == ["新"]


async def test_confirmed_result_waits_for_session_then_delivers(flow):
    task = flow.tasks.create_task("处理活动邀请")
    operation = flow.drafts.save_reply_draft(task["task_id"], "m1", "thread-1", **DRAFT)
    assert flow.sender.calls == []

    execution = flow.confirmations.confirm_reply(task["task_id"], operation["operation_id"], 1)
    assert execution["status"] == "sent"
    assert len(flow.sender.calls) == 1

    runs = flow.service.list_runs(task["task_id"])
    assert [(run["kind"], run["status"]) for run in runs] == [("execution_result", "pending")]
    assert flow.gateway.calls_of("execution_result") == []

    flow.tasks.bind_sdk_session(task["task_id"], "sdk-1")
    flow.service.kick()
    assert await wait_for(lambda: flow.gateway.calls_of("execution_result"))
    assert flow.gateway.calls_of("execution_result") == [
        {
            "kind": "execution_result",
            "task_id": task["task_id"],
            "sdk_session_id": "sdk-1",
            "operation_id": operation["operation_id"],
            "version": 1,
            "result": {"status": "sent", "message_id": "sent-1"},
        }
    ]
    assert await wait_for(lambda: flow.service.list_runs(task["task_id"])[0]["status"] == "done")


async def test_duplicate_confirmation_sends_once_and_registers_one_delivery(flow):
    task = flow.tasks.create_task("重复确认")
    operation = flow.drafts.save_reply_draft(task["task_id"], "m1", "thread-1", **DRAFT)

    first = flow.confirmations.confirm_reply(task["task_id"], operation["operation_id"], 1)
    second = flow.confirmations.confirm_reply(task["task_id"], operation["operation_id"], 1)

    assert second == first
    assert len(flow.sender.calls) == 1
    deliveries = [
        run for run in flow.service.list_runs(task["task_id"]) if run["kind"] == "execution_result"
    ]
    assert len(deliveries) == 1


async def test_delivery_failure_keeps_send_result_and_no_resend(flow):
    task = flow.tasks.create_task("回传通道故障")
    operation = flow.drafts.save_reply_draft(task["task_id"], "m1", "thread-1", **DRAFT)
    flow.confirmations.confirm_reply(task["task_id"], operation["operation_id"], 1)
    flow.tasks.bind_sdk_session(task["task_id"], "sdk-1")

    def broken(**kwargs):
        async def events():
            yield {"type": "text", "text": "收到执行结果"}
            raise RuntimeError("回传通道故障")

        return events()

    flow.gateway.handle("execution_result", broken)
    flow.service.kick()
    assert await wait_for(lambda: flow.service.list_runs(task["task_id"])[0]["status"] == "error")

    execution = flow.confirmations.get_execution(operation["operation_id"])
    assert execution["status"] == "sent"
    assert execution["result"] == {"status": "sent", "message_id": "sent-1"}
    assert (
        flow.confirmations.confirm_reply(task["task_id"], operation["operation_id"], 1) == execution
    )
    assert len(flow.sender.calls) == 1

    runs = flow.service.list_runs(task["task_id"])
    assert [run["kind"] for run in runs] == ["execution_result"]
    flow.service.kick()
    await drain(flow.service)
    assert len(flow.gateway.calls_of("execution_result")) == 1


@pytest.mark.parametrize(
    "returned",
    [
        {"status": "failed", "reason": "明确拒绝"},
        {"status": "unknown", "reason": "网络中断"},
        {"status": []},
    ],
)
async def test_result_variants_delivered_without_resend(flow, returned):
    task = flow.service.accept_new_mail("variant", "thread")
    await drain(flow.service)
    op = flow.drafts.save_reply_draft(task["task_id"], "variant", "thread", **DRAFT)
    calls = []

    def sender(**fields):
        calls.append(fields)
        return returned

    flow.confirmations.send_reply = sender
    for _ in range(2):
        flow.confirmations.accept_confirmation(task["task_id"], op["operation_id"], 1)
        flow.service.confirm_execution(op["operation_id"])
    await drain(flow.service)
    expected = returned["status"] if isinstance(returned["status"], str) else "unknown"
    assert flow.confirmations.get_execution(op["operation_id"])["status"] == expected
    assert len(calls) == 1
    assert len(flow.gateway.calls_of("execution_result")) == 1
    assert flow.gateway.calls_of("execution_result")[0]["result"]["status"] == expected


async def test_result_and_delivery_rollback_together(flow, monkeypatch):
    from server.sessions import runs

    task = flow.tasks.create_task("原子保存")
    op = flow.drafts.save_reply_draft(task["task_id"], "rollback", "thread", **DRAFT)

    def fail(*args, **kwargs):
        raise RuntimeError("回传登记失败")

    monkeypatch.setattr(runs, "insert", fail)
    with pytest.raises(RuntimeError, match="回传登记失败"):
        flow.confirmations.confirm_reply(task["task_id"], op["operation_id"], 1)
    assert flow.confirmations.get_execution(op["operation_id"])["status"] == "sending"
    assert flow.service.list_runs(task["task_id"]) == []
    assert len(flow.sender.calls) == 1
    flow.confirmations.confirm_reply(task["task_id"], op["operation_id"], 1)
    assert len(flow.sender.calls) == 1


async def test_shutdown_interrupts_agent_without_replaying(flow):
    entered = asyncio.Event()

    async def blocked(**kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield {"type": "done"}

    flow.gateway.handle("message", blocked)
    task = flow.tasks.create_task("中断")
    run = flow.service.submit_message(task["task_id"], "等待")
    await asyncio.wait_for(entered.wait(), 5)
    await flow.service.close()
    restarted = GatewayRuntime(flow.gateway, confirmations=flow.confirmations)
    try:
        assert restarted.resume() == [run["run_id"]]
        assert restarted.get_run(run["run_id"])["status"] == "interrupted"
        assert len(flow.gateway.calls_of("message")) == 1
    finally:
        await restarted.close()
