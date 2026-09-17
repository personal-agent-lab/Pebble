"""后台记忆回顾调度：触发计数、空闲门控、优先级、恢复与手动入口。

真实落盘 SQLite 与真实回顾登记；Agent 用替身，回顾调用可脚本化阻塞。
"""

import asyncio
from typing import NamedTuple

import pytest

from server.db import init_db, session
from server.gateway.runtime import GatewayRuntime
from server.memory.review import MemoryReviewScheduler
from server.sessions.service import SessionStore
from tests.support import memory_anchor, seed_memory
from tests.support.agent_double import FakeAgentGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def drain(service):
    async with asyncio.timeout(5):
        while service._active or service._review_tasks:
            await asyncio.gather(
                *list(service._active.values()), *list(service._review_tasks.values())
            )


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


class ReviewFlow(NamedTuple):
    service: GatewayRuntime
    gateway: FakeAgentGateway
    tasks: SessionStore


@pytest.fixture
async def review_flow(settings):
    init_db()
    gateway = FakeAgentGateway()
    service = GatewayRuntime(gateway, reviews=MemoryReviewScheduler())
    try:
        yield ReviewFlow(service=service, gateway=gateway, tasks=SessionStore())
    finally:
        await service.close()


def review_rows() -> list[dict]:
    with session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM memory_reviews ORDER BY rowid")]


def timeline_count(task_id: str) -> int:
    with session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM task_timeline_items WHERE task_id = ?", (task_id,)
        ).fetchone()["n"]


async def send(flow: ReviewFlow, task_id: str, *texts: str) -> None:
    for text in texts:
        flow.service.submit_message(task_id, text)
        await drain(flow.service)


async def submit_and_settle(flow: ReviewFlow, task_id: str, text: str) -> None:
    """提交一条消息并等它完成，不等可能被脚本阻塞的回顾任务。"""
    flow.service.submit_message(task_id, text)
    await wait_for(
        lambda: (
            (run := flow.service.latest_run(task_id))["status"] == "done"
            and run["kind"] == "message"
        )
    )


async def test_review_runs_after_five_done_messages_and_writes_nothing_visible(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    await send(review_flow, task_id, "第一句", "第二句", "第三句", "第四句")
    subscription = review_flow.service.events.subscribe(task_id)
    await send(review_flow, task_id, "我最近正在学习 Hermes 的设计")

    calls = review_flow.gateway.review_calls
    assert len(calls) == 1
    assert calls[0]["task_id"] == task_id
    for text in ["第一句", "第二句", "第三句", "第四句", "我最近正在学习 Hermes 的设计"]:
        assert f"用户：{text}" in calls[0]["transcript"]
    assert "助手：收到：" in calls[0]["transcript"]
    assert calls[0]["instructions"]

    rows = review_rows()
    assert [(r["status"], r["origin"]) for r in rows] == [("done", "interval")]

    # 对用户不可见：时间线只有五轮对话文本，SSE 只收到消息轮的事件。
    assert timeline_count(task_id) == 10
    events = []
    while not subscription.empty():
        events.append(subscription.get_nowait())
    assert events and all(event["run_id"] for event in events)
    run = review_flow.service.latest_run(task_id)
    assert (run["kind"], run["status"]) == ("message", "done")


async def test_review_consolidation_notice_reaches_timeline_and_events(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    store = review_flow.service.memory_store
    seed_memory(store, "user", "- 回答先给结论\n- 回答要先说结论")

    async def consolidating_review(task_id, instructions, transcript):
        line = memory_anchor(store, "- 回答要先说结论")
        arguments = {"operations": [{"action": "delete", "anchor": line}]}
        removed = store.edit(**arguments)
        return [{"tool": "memory_edit", "arguments": arguments, "result": removed}]

    review_flow.gateway.review_handler = consolidating_review
    await send(review_flow, task_id, "一", "二", "三", "四")
    subscription = review_flow.service.events.subscribe(task_id)
    await send(review_flow, task_id, "五")

    assert store.snapshot()["user"]["content"] == "- 回答先给结论"
    items = review_flow.service.get_timeline(task_id)["items"]
    assert items[-1]["kind"] == "notice"
    assert items[-1]["text"] == "整理记忆：已删除：- 回答要先说结论"
    events = []
    while not subscription.empty():
        events.append(subscription.get_nowait())
    notice = events[-1]
    assert (notice["type"], notice["text"], notice["item_id"]) == (
        "notice",
        "整理记忆：已删除：- 回答要先说结论",
        items[-1]["item_id"],
    )
    assert notice["run_id"] == review_flow.service.latest_run(task_id)["run_id"]


async def test_fewer_than_five_messages_does_not_trigger(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    await send(review_flow, task_id, "一", "二", "三", "四")
    await asyncio.sleep(0.05)
    assert review_flow.gateway.review_calls == []
    assert review_rows() == []


async def test_review_waits_until_task_is_idle(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    await send(review_flow, task_id, "一", "二", "三", "四")
    gates = [asyncio.Event(), asyncio.Event()]
    seen: list[int] = []

    async def scripted(turn):
        seen.append(len(seen) + 1)
        if len(seen) <= 2:  # 第 5、6 条消息等待放行
            await gates[len(seen) - 1].wait()
        yield {"type": "session", "sdk_session_id": turn.sdk_session_id or "s"}
        yield {"type": "text", "text": f"收到：{turn.message}"}
        yield {"type": "done"}

    review_flow.gateway.handle("message", scripted)
    review_flow.service.submit_message(task_id, "五")
    review_flow.service.submit_message(task_id, "六")
    await wait_for(lambda: len(review_flow.gateway.calls_of("message")) == 5)

    gates[0].set()
    await wait_for(lambda: any(r["status"] == "pending" for r in review_rows()))
    # 第 6 条消息仍在等待执行：回顾已登记但不启动（时间线上只有它的用户文本）。
    assert review_flow.gateway.review_calls == []
    assert timeline_count(task_id) == 11

    gates[1].set()
    await drain(review_flow.service)
    calls = review_flow.gateway.review_calls
    assert len(calls) == 1
    # 窗口在登记时冻结：只覆盖前五轮，不含第 6 条。
    assert "用户：五" in calls[0]["transcript"]
    assert "用户：六" not in calls[0]["transcript"]


async def test_message_during_review_is_not_delayed(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    release = asyncio.Event()

    async def blocked_review(task_id, instructions, transcript):
        await release.wait()
        return []

    review_flow.gateway.review_handler = blocked_review
    await send(review_flow, task_id, "一", "二", "三", "四")
    await submit_and_settle(review_flow, task_id, "五")
    await wait_for(lambda: len(review_flow.gateway.review_calls) == 1)

    review_flow.service.submit_message(task_id, "六")
    await wait_for(lambda: len(review_flow.gateway.calls_of("message")) == 6)
    # 回顾仍在执行，第 6 条消息已经完成。
    await wait_for(
        lambda: (
            (run := review_flow.service.latest_run(task_id))["status"] == "done"
            and run["kind"] == "message"
        )
    )
    assert len(review_flow.gateway.review_calls) == 1

    release.set()
    await drain(review_flow.service)
    assert [(r["status"], r["origin"]) for r in review_rows()] == [("done", "interval")]


async def test_review_retriggers_with_fresh_window(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    await send(review_flow, task_id, "一", "二", "三", "四", "五")
    await send(review_flow, task_id, "六", "七", "八", "九", "十")
    calls = review_flow.gateway.review_calls
    assert len(calls) == 2
    assert "用户：一" in calls[0]["transcript"] and "用户：十" not in calls[0]["transcript"]
    assert "用户：一" not in calls[1]["transcript"] and "用户：十" in calls[1]["transcript"]
    assert [r["status"] for r in review_rows()] == ["done", "done"]


async def test_interrupted_review_self_heals_after_restart(review_flow, settings):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    release = asyncio.Event()

    async def blocked_review(task_id, instructions, transcript):
        await release.wait()
        return []

    review_flow.gateway.review_handler = blocked_review
    await send(review_flow, task_id, "一", "二", "三", "四")
    await submit_and_settle(review_flow, task_id, "五")
    await wait_for(lambda: len(review_flow.gateway.review_calls) == 1)
    await review_flow.service.close()  # 模拟进程退出：回顾中断，行仍是 running

    service = GatewayRuntime(review_flow.gateway, reviews=MemoryReviewScheduler())
    try:
        interrupted = service.resume()
        assert len(interrupted) == 1
        assert review_rows()[0]["status"] == "interrupted"

        release.set()
        await send(ReviewFlow(service, review_flow.gateway, review_flow.tasks), task_id, "六")
        assert len(review_flow.gateway.review_calls) == 2
        assert [r["status"] for r in review_rows()] == ["interrupted", "done"]
    finally:
        await service.close()


async def test_pending_review_runs_after_startup(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    await send(review_flow, task_id, "一", "二")
    scheduler = MemoryReviewScheduler()
    scheduler.enqueue_manual(task_id)
    await review_flow.service.close()

    service = GatewayRuntime(review_flow.gateway, reviews=MemoryReviewScheduler())
    try:
        service.resume()
        await drain(service)
        assert len(review_flow.gateway.review_calls) == 1
        assert review_rows()[0]["status"] == "done"
    finally:
        await service.close()


async def test_manual_review_covers_whole_task_immediately(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    await send(review_flow, task_id, "一", "二")
    row = review_flow.service.submit_memory_review(task_id)
    assert (row["status"], row["origin"]) == ("pending", "manual")
    await drain(review_flow.service)
    calls = review_flow.gateway.review_calls
    assert len(calls) == 1
    assert "用户：一" in calls[0]["transcript"] and "用户：二" in calls[0]["transcript"]
    assert review_rows()[0]["status"] == "done"


async def test_manual_review_returns_existing_open_review(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]
    release = asyncio.Event()

    async def blocked_review(task_id, instructions, transcript):
        await release.wait()
        return []

    review_flow.gateway.review_handler = blocked_review
    await send(review_flow, task_id, "一", "二", "三", "四")
    await submit_and_settle(review_flow, task_id, "五")
    await wait_for(lambda: review_rows() and review_rows()[0]["status"] == "running")
    second = review_flow.service.submit_memory_review(task_id)
    assert second["review_id"] == review_rows()[0]["review_id"]

    release.set()
    await drain(review_flow.service)
    assert len(review_flow.gateway.review_calls) == 1


async def test_review_failure_is_recorded_without_side_effects(review_flow):
    task_id = review_flow.tasks.create_task("闲聊")["task_id"]

    async def failing_review(task_id, instructions, transcript):
        raise RuntimeError("模型不可用")

    review_flow.gateway.review_handler = failing_review
    await send(review_flow, task_id, "一", "二", "三", "四", "五")
    rows = review_rows()
    assert rows[0]["status"] == "error"
    assert "记忆回顾失败" in rows[0]["error"]
    assert timeline_count(task_id) == 10


async def test_reviews_of_different_tasks_run_concurrently(review_flow):
    first = review_flow.tasks.create_task("闲聊一")["task_id"]
    second = review_flow.tasks.create_task("闲聊二")["task_id"]
    await send(review_flow, first, "一一", "一二", "一三", "一四", "一五")
    await send(review_flow, second, "二一", "二二", "二三", "二四", "二五")
    assert len(review_flow.gateway.review_calls) == 2
    assert {call["task_id"] for call in review_flow.gateway.review_calls} == {first, second}
