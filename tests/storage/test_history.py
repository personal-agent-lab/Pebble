"""历史对话检索：增量同步、跨任务搜索、默认排除当前任务、前后文与邮件执行状态。

对话由脚本化的 Agent 替身经真实调度产生，SQLite 与时间线都是真的。
"""

import asyncio

import pytest

from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, build_tools, exposed_tools
from server.approval.service import ConfirmationService
from server.db import init_db, session
from server.errors import HistoryValidationError, NotFoundError
from server.gateway.runtime import GatewayRuntime
from server.sessions.history import HistoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from tests.support.agent_double import FakeAgentGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def drain(service):
    async with asyncio.timeout(5):
        while service._active or service._titles:
            await asyncio.gather(*list(service._active.values()), *list(service._titles))


@pytest.fixture
async def world(settings):
    init_db()
    gateway = FakeAgentGateway()
    service = GatewayRuntime(gateway)
    try:
        yield gateway, service, SessionStore(), HistoryStore()
    finally:
        await service.close()


def reply_with(gateway, text):
    def handler(turn):
        async def events():
            yield {"type": "session", "sdk_session_id": turn.sdk_session_id or "s"}
            yield {"type": "text", "text": text}
            yield {"type": "done"}

        return events()

    gateway.handle("message", handler)


async def say(service, gateway, task_id, message, answer):
    reply_with(gateway, answer)
    service.submit_message(task_id, message)
    await drain(service)


async def test_search_finds_past_decisions_newest_first_and_excludes_the_current_task(world):
    gateway, service, tasks, history = world
    plan = tasks.create_task("方案讨论")["task_id"]
    await say(
        service,
        gateway,
        plan,
        "A 方案和 B 方案怎么选？",
        "A 方案依赖太重，建议放弃 A 方案，改用 B 方案。",
    )
    await say(service, gateway, plan, "好，就放弃 A 方案。", "已按你的决定改用 B 方案。")
    current = tasks.create_task("新问题")["task_id"]
    await say(service, gateway, current, "上次为什么放弃 A 方案？", "我查一下。")

    found = history.search("放弃 A 方案", exclude_task_id=current)["results"]

    # 全部词命中才算命中：提问里没有“放弃”，最后一句回答也没有
    assert [item["task_id"] for item in found] == [plan] * 2
    assert [item["speaker"] for item in found] == ["user", "assistant"]
    assert found[0]["task_title"] and "放弃 A 方案" in found[0]["snippet"]
    # 不排除时当前任务里的提问也会命中
    assert current in {item["task_id"] for item in history.search("放弃 A 方案")["results"]}
    # 1–2 个字的词走包含匹配；中英文与编号都能检索
    assert history.search("B 方案", exclude_task_id=current)["results"]
    assert history.search("没有讨论过的事情")["results"] == []


async def test_running_turns_are_not_indexed_and_deleted_tasks_disappear(world, settings):
    gateway, service, tasks, history = world
    task = tasks.create_task("进行中")["task_id"]
    started = asyncio.Event()
    release = asyncio.Event()

    def handler(turn):
        async def events():
            yield {"type": "session", "sdk_session_id": "s"}
            yield {"type": "text", "text": "正在整理 CORAL-7421 的资料"}
            started.set()
            await release.wait()
            yield {"type": "done"}

        return events()

    gateway.handle("message", handler)
    service.submit_message(task, "整理一下")
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.05)
    assert history.search("CORAL-7421")["results"] == []
    release.set()
    await drain(service)
    assert history.search("CORAL-7421")["results"]

    tasks.delete_task(task)
    assert history.search("CORAL-7421")["results"] == []
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM history_items").fetchone()["n"] == 0


async def test_read_returns_context_and_mail_status_follows_later_changes(world):
    gateway, service, tasks, history = world
    drafts = MailDraftStore()
    task = tasks.create_task("约张老师")["task_id"]

    def handler(turn):
        async def events():
            yield {"type": "session", "sdk_session_id": "s"}
            yield {"type": "text", "text": "我先起草一封给张老师的信。"}
            saved = drafts.save_email_draft(
                turn.task_id, ["zhang@example.com"], "课程讨论时间", "周四下午三点可以吗？"
            )
            yield {"type": "draft_saved", **saved}
            yield {"type": "text", "text": "草稿在上面，确认后发送。"}
            yield {"type": "done"}

        return events()

    gateway.handle("message", handler)
    service.submit_message(task, "给张老师写信约周四")
    await drain(service)

    hit = history.search("课程讨论时间")["results"][0]
    assert hit["speaker"] == "mail_draft"
    window = history.read(task, hit["item_id"], before=1, after=1)
    assert [item["speaker"] for item in window["items"]] == ["assistant", "mail_draft", "assistant"]
    card = window["items"][1]
    assert card["matched"] is True
    assert card["mail_draft"]["status"] == "pending"
    assert card["mail_draft"]["result"] is None
    assert window["has_earlier"] is True and window["has_later"] is False

    # 草稿被改成新版本：索引重写，旧正文不再命中，新正文命中
    operation_id = tasks.list_task_operations(task)[0]["operation_id"]
    current = drafts.get_draft(operation_id)
    drafts.update_draft(
        operation_id, current["version"], current["to"], current["subject"], "改到周五上午十点。"
    )
    # 确认发送之后，读取到的状态与执行结果随之更新
    confirmations = ConfirmationService(lambda **_: {"status": "sent", "message_id": "m-1"})
    latest = drafts.get_draft(operation_id)
    confirmations.accept_confirmation(task, operation_id, latest["version"])
    confirmations.execute_accepted(operation_id, deliver=False)
    sent = history.read(task, hit["item_id"], before=0, after=0)["items"][0]["mail_draft"]
    assert sent["status"] == "sent"
    assert sent["result"] == {"status": "sent", "message_id": "m-1"}
    assert history.search("周四下午三点")["results"] == []
    assert history.search("周五上午十点")["results"][0]["item_id"] == hit["item_id"]


async def test_date_range_and_input_validation(world):
    gateway, service, tasks, history = world
    task = tasks.create_task("预算")["task_id"]
    await say(service, gateway, task, "运维预算怎么定？", "下季度运维预算提高一成。")

    assert history.search("运维预算", after="2000-01-01", before="2999-12-31")["results"]
    assert history.search("运维预算", before="2000-01-01")["results"] == []
    assert history.search("运维预算", after="2999-01-01")["results"] == []
    for kwargs in ({"after": "昨天"}, {"before": "2026-02-30"}, {"max_results": 0}):
        with pytest.raises(HistoryValidationError):
            history.search("运维预算", **kwargs)
    with pytest.raises(HistoryValidationError):
        history.search("  ")
    with pytest.raises(NotFoundError):
        history.read(task, "missing-item")
    with pytest.raises(NotFoundError):
        history.read("missing-task", "missing-item")


def test_history_tools_are_readonly_in_every_turn_and_exclude_the_current_task(settings):
    init_db()
    tools = build_tools(ToolDeps(drafts=None, tasks=None, gmail=None, history=HistoryStore()))
    for kind in TurnKind:
        names = {tool.name for tool in exposed_tools(tools, allowed=ALLOWED_EFFECTS[kind])}
        assert {"history_search", "history_read"} <= names
    search = next(tool for tool in tools if tool.name == "history_search")
    assert search.needs_task_id is True
    assert "task_id" not in search.parameters_schema["properties"]
    # 读取需要显式给出过去任务的 task_id，由模型取自检索结果
    read = next(tool for tool in tools if tool.name == "history_read")
    assert read.parameters_schema["required"] == ["task_id", "item_id"]
    # 没有装配历史存储时不出现历史工具
    unwired = build_tools(ToolDeps(drafts=None, tasks=None, gmail=None))
    assert not [tool for tool in unwired if tool.name.startswith("history_")]
