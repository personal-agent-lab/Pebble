"""每轮记忆判断接线：并行触发、程序提示落库与推送、失败与重启行为。

真实落盘 SQLite 与真实 MemoryStore；Agent 用替身，判断调用可脚本化阻塞。
"""

import asyncio
from typing import NamedTuple

import pytest

from server.db import init_db, session
from server.gateway.runtime import GatewayRuntime
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from tests.support.agent_double import FakeAgentGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def drain(service):
    async with asyncio.timeout(5):
        while service._active or service._review_tasks or service._judges:
            await asyncio.gather(
                *list(service._active.values()),
                *list(service._review_tasks.values()),
                *list(service._judges),
            )


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


class JudgeFlow(NamedTuple):
    service: GatewayRuntime
    gateway: FakeAgentGateway
    tasks: SessionStore
    store: MemoryStore


@pytest.fixture
async def judge_flow(settings):
    init_db()
    gateway = FakeAgentGateway()
    store = MemoryStore(settings.data_dir)
    service = GatewayRuntime(gateway, memory_store=store)
    try:
        yield JudgeFlow(service=service, gateway=gateway, tasks=SessionStore(), store=store)
    finally:
        await service.close()


def timeline_items(task_id: str) -> list[dict]:
    with session() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_timeline_items WHERE task_id = ? ORDER BY rowid", (task_id,)
            )
        ]


def notices(task_id: str) -> list[str]:
    return [item["text"] for item in timeline_items(task_id) if item["kind"] == "notice"]


async def send(flow: JudgeFlow, task_id: str, *texts: str) -> None:
    for text in texts:
        flow.service.submit_message(task_id, text)
        await drain(flow.service)


async def test_judgment_runs_in_parallel_and_notice_reaches_timeline(judge_flow):
    task_id = judge_flow.tasks.create_task("闲聊")["task_id"]
    release = asyncio.Event()

    async def blocked_judge(task_id, instructions, message):
        await release.wait()
        arguments = {
            "operations": [{"action": "append", "target": "user", "text": "用户周末常去徒步"}]
        }
        result = judge_flow.store.edit(**arguments)
        return [{"tool": "memory_edit", "arguments": arguments, "result": result}]

    judge_flow.gateway.judge_handler = blocked_judge
    subscription = judge_flow.service.events.subscribe(task_id)
    judge_flow.service.submit_message(task_id, "我周末常去徒步")
    await wait_for(lambda: len(judge_flow.gateway.judge_calls) == 1)
    # 判断仍在执行，主回答已经完成：判断不占用任务的串行调度。
    await wait_for(
        lambda: (
            (run := judge_flow.service.latest_run(task_id))["status"] == "done"
            and run["kind"] == "message"
        )
    )
    assert notices(task_id) == []

    release.set()
    await drain(judge_flow.service)
    assert notices(task_id) == ["已记住：用户周末常去徒步"]
    notice_items = [item for item in timeline_items(task_id) if item["kind"] == "notice"]
    assert notice_items[0]["role"] is None
    # 提示挂在触发它的消息轮上，并经 SSE 推送。
    run = judge_flow.service.latest_run(task_id)
    assert notice_items[0]["run_id"] == run["run_id"]
    events = []
    while not subscription.empty():
        events.append(subscription.get_nowait())
    notice_events = [event for event in events if event["type"] == "notice"]
    assert notice_events == [
        {
            "run_id": run["run_id"],
            "item_id": notice_items[0]["item_id"],
            "type": "notice",
            "text": "已记住：用户周末常去徒步",
        }
    ]
    assert judge_flow.store.snapshot()["user"]["content"] == "用户周末常去徒步"


async def test_no_change_produces_no_notice(judge_flow):
    task_id = judge_flow.tasks.create_task("闲聊")["task_id"]
    await send(judge_flow, task_id, "今天天气怎么样")
    assert len(judge_flow.gateway.judge_calls) == 1
    assert "今天天气怎么样" in judge_flow.gateway.judge_calls[0]["message"]
    assert notices(task_id) == []
    assert [item["kind"] for item in timeline_items(task_id)] == ["text", "text"]


async def test_judgment_failure_does_not_affect_message(judge_flow):
    task_id = judge_flow.tasks.create_task("闲聊")["task_id"]

    async def failing_judge(task_id, instructions, message):
        raise RuntimeError("模型不可用")

    judge_flow.gateway.judge_handler = failing_judge
    await send(judge_flow, task_id, "我最近在读苏轼")
    run = judge_flow.service.latest_run(task_id)
    assert (run["kind"], run["status"]) == ("message", "done")
    assert [item["kind"] for item in timeline_items(task_id)] == ["text", "text"]


async def test_new_mail_turn_does_not_trigger_judgment(judge_flow):
    judge_flow.service.accept_new_mail("msg-1", "thread-1")
    await drain(judge_flow.service)
    assert judge_flow.gateway.judge_calls == []


async def test_next_turn_judgment_sees_system_notices(judge_flow):
    task_id = judge_flow.tasks.create_task("闲聊")["task_id"]

    async def asking_judge(task_id, instructions, message):
        return [
            {
                "tool": "memory_ask",
                "arguments": {"question": "你说的Hermes是指哪个项目？"},
                "result": {"question": "你说的Hermes是指哪个项目？"},
            }
        ]

    judge_flow.gateway.judge_handler = asking_judge
    await send(judge_flow, task_id, "我在看 Hermes")
    assert notices(task_id) == ["想确认：你说的Hermes是指哪个项目？"]

    judge_flow.gateway.judge_handler = None
    await send(judge_flow, task_id, "是写 Agent 的那个")
    second = judge_flow.gateway.judge_calls[1]["message"]
    assert "## 用户刚发的消息\n是写 Agent 的那个" in second
    assert "系统：想确认：你说的Hermes是指哪个项目？" in second


async def test_restart_drops_in_flight_judgment(judge_flow, settings):
    task_id = judge_flow.tasks.create_task("闲聊")["task_id"]
    release = asyncio.Event()

    async def blocked_judge(task_id, instructions, message):
        await release.wait()
        return []

    judge_flow.gateway.judge_handler = blocked_judge
    judge_flow.service.submit_message(task_id, "你好")
    await wait_for(lambda: len(judge_flow.gateway.judge_calls) == 1)
    await judge_flow.service.close()  # 模拟进程退出：进行中的判断直接丢弃

    service = GatewayRuntime(judge_flow.gateway, memory_store=judge_flow.store)
    try:
        interrupted = service.resume()
        assert interrupted == []  # 消息轮已完成，判断无状态不恢复
        assert notices(task_id) == []
        release.set()
        await drain(service)
        # 重启后的新消息才触发新判断；被丢弃的那次不会补发提示。
        service.submit_message(task_id, "还在吗")
        await drain(service)
        assert len(judge_flow.gateway.judge_calls) == 2
        assert notices(task_id) == []
    finally:
        await service.close()
