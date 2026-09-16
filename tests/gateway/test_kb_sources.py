"""资料读取的来源记录：只有真实读取产生来源，来源挂在该轮最后一段回答上。

工具调用走真实的进程内 MCP 端点，事件按真实网关的方式从队列取出后再交给运行时；
SQLite、KbStore 与时间线都是真的，只有 Agent 子进程是替身。
"""

import asyncio
from typing import NamedTuple

import pytest

from server.agent.mcp import ToolServer
from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, build_tools, exposed_tools
from server.db import init_db
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.tools.personal_kb.service import KbStore
from server.tools.personal_kb.tools import EXCERPT_LIMIT
from tests.support.agent_double import FakeAgentGateway
from tests.support.mcp_http import mcp_session, tool_payload

pytestmark = pytest.mark.anyio
BASE_URL = "http://127.0.0.1:8000"
BODY = "## 验收结果\n\n本次验收代号 CORAL-7421，结论为通过。"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def drain(service):
    async with asyncio.timeout(5):
        while service._active or service._titles:
            await asyncio.gather(*list(service._active.values()), *list(service._titles))


class Flow(NamedTuple):
    service: GatewayRuntime
    gateway: FakeAgentGateway
    tasks: SessionStore
    kb: KbStore
    tools: list


@pytest.fixture
async def flow(settings):
    init_db()
    gateway = FakeAgentGateway()
    kb = KbStore(settings.data_dir)
    tools = build_tools(ToolDeps(drafts=None, tasks=None, gmail=None, kb_store=kb))
    service = GatewayRuntime(gateway)
    try:
        yield Flow(service=service, gateway=gateway, tasks=SessionStore(), kb=kb, tools=tools)
    finally:
        await service.close()


async def call(flow: Flow, task_id: str, name: str, arguments: dict) -> tuple[list[dict], object]:
    """按真实网关的方式调一次工具：走本轮 MCP 端点，再取回该工具入队的事件。"""
    visible = exposed_tools(flow.tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
    queued: asyncio.Queue = asyncio.Queue()
    server = ToolServer()
    async with (
        server.serve(visible, task_id=task_id, queued=queued) as path,
        mcp_session(server, f"{BASE_URL}{path}") as session,
    ):
        result = await session.call_tool(name, arguments)
    events = []
    while not queued.empty():
        events.append(queued.get_nowait())
    return events, result


def first_ref(flow: Flow) -> dict:
    return flow.kb.search(query="CORAL-7421")["results"][0]["ref"]


async def test_only_successful_reads_produce_sources(flow, settings):
    task = flow.tasks.create_task("问资料")
    saved = flow.kb.save(title="验收纪要", body=BODY)
    ref = first_ref(flow)

    events, result = await call(flow, task["task_id"], "kb_read", {"ref": ref})

    assert tool_payload(result)["body"].startswith("## 验收结果")
    assert [event["type"] for event in events] == ["source"]
    source = events[0]["source"]
    assert source["ref"] == ref
    assert source["title"] == "验收纪要"
    assert source["excerpt"] == tool_payload(result)["body"]

    # 检索只给摘要，摘要不作为引用
    events, result = await call(flow, task["task_id"], "kb_search", {"query": "CORAL-7421"})
    assert tool_payload(result)["results"]
    assert events == []

    # 失败的读取没有来源
    events, result = await call(
        flow, task["task_id"], "kb_read", {"ref": {**ref, "lines": [1, 9999]}}
    )
    assert result.isError is True
    assert tool_payload(result)["error"] == "invalid_kb"
    assert events == []

    # 整篇读取也留下来源，引用覆盖整个文件
    text_lines = (settings.data_dir / saved["path"]).read_text(encoding="utf-8").split("\n")
    events, result = await call(flow, task["task_id"], "kb_read", {"path": saved["path"]})
    assert events[0]["source"]["ref"]["lines"] == [1, len(text_lines)]
    assert events[0]["source"]["ref"]["commit"] == saved["version"]


async def test_long_reads_are_capped_in_the_source_excerpt(flow):
    task = flow.tasks.create_task("问资料")
    flow.kb.save(title="长文", body="## 长文\n\n" + "很长的正文内容。" * 200)
    ref = flow.kb.search(query="很长的正文")["results"][0]["ref"]

    events, result = await call(flow, task["task_id"], "kb_read", {"ref": ref})

    assert len(tool_payload(result)["body"]) > EXCERPT_LIMIT
    excerpt = events[0]["source"]["excerpt"]
    assert len(excerpt) == EXCERPT_LIMIT + 1 and excerpt.endswith("…")


async def test_sources_attach_to_the_last_answer_of_the_run(flow):
    task = flow.tasks.create_task("问资料")
    flow.kb.save(title="验收纪要", body=BODY)
    ref = first_ref(flow)

    def handler(turn):
        async def events():
            yield {"type": "text", "text": "我先存一份新资料。"}
            saved_events, _ = await call(
                flow,
                turn.task_id,
                "kb_save",
                {"title": "临时记录", "body": "## 分节\n\n临时正文。"},
            )
            for event in saved_events:
                yield event
            yield {"type": "text", "text": "再查一下已有的资料。"}
            read_events, _ = await call(flow, turn.task_id, "kb_read", {"ref": ref})
            for event in read_events:
                yield event
            yield {"type": "text", "text": "资料里写的代号是 CORAL-7421。"}
            yield {"type": "done"}

        return events()

    flow.gateway.handle("message", handler)
    flow.service.submit_message(task["task_id"], "验收代号是什么？")
    await drain(flow.service)

    items = flow.service.get_timeline(task["task_id"])["items"]
    answers = [item for item in items if item["kind"] == "text" and item["role"] == "assistant"]

    assert [item["text"] for item in answers] == [
        "我先存一份新资料。",
        "再查一下已有的资料。资料里写的代号是 CORAL-7421。",
    ]
    # 来源不打断回答本身，但统一挂在该轮最后一段回答上，而不是产生它的那一段
    assert "sources" not in answers[0]
    assert answers[-1]["sources"][0]["ref"] == ref
    assert answers[-1]["sources"][0]["excerpt"].startswith("## 验收结果")
    # 程序提示与来源各归各类：保存提示不是来源
    assert [item["kind"] for item in items] == ["text", "text", "notice", "text"]


async def test_repeated_reads_are_deduplicated_and_stable_across_refreshes(flow):
    task = flow.tasks.create_task("问资料")
    flow.kb.save(title="验收纪要", body=BODY)
    flow.kb.save(title="设备清单", body="## 设备\n\n离心机 CO-7100 两台。")
    ref = first_ref(flow)
    other = flow.kb.search(query="离心机")["results"][0]["ref"]

    def handler(turn):
        async def events():
            for target in (ref, other, ref):
                queued, _ = await call(flow, turn.task_id, "kb_read", {"ref": target})
                for event in queued:
                    yield event
            yield {"type": "text", "text": "两份资料都看过了。"}
            yield {"type": "done"}

        return events()

    flow.gateway.handle("message", handler)
    flow.service.submit_message(task["task_id"], "帮我核对两份资料")
    await drain(flow.service)

    first_read = flow.service.get_timeline(task["task_id"])
    sources = first_read["items"][-1]["sources"]

    # 同一版本与行号重复读取只记一次，读取顺序保持
    assert [item["ref"] for item in sources] == [ref, other]
    assert flow.service.get_timeline(task["task_id"]) == first_read
