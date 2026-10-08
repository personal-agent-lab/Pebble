"""观测采集：材料摘要、请求级用量、SDK 结果、工具步骤与降级在真实轮次里落库。

契约 docs/observability.md：观测只补运行事实，参数与返回正文仍只在时间线；
权限拒绝与执行失败在步骤状态上区分；缺字段留空不补零。
"""

import asyncio
import json

from qodercn_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from server.agent import client as agent_client
from server.agent.client import QoderGateway, tool_trace_hooks
from server.agent.context import Material
from server.agent.toolset import TurnKind
from server.db import session
from server.gateway.agent_contract import Turn
from server.sessions.observations import TurnObserver
from server.sessions.service import SessionStore
from tests.gateway.test_agent_stream import (
    ToolCall,
    collect,
    install_sdk,
    make_gateway,
    result,
)
from tests.gateway.test_trajectory import seed_turn


def summary(run_id: str) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM run_observations WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def steps(run_id: str) -> list[dict]:
    with session() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM observation_steps WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            )
        ]


def observed_turn(task_id: str, run_id: str, **turn_fields) -> Turn:
    return Turn(
        kind=TurnKind.MESSAGE,
        task_id=task_id,
        sdk_session_id=None,
        message="测试输入",
        run_id=run_id,
        **turn_fields,
    )


def test_stream_records_materials_usage_and_result(settings, monkeypatch):
    gateway = make_gateway(settings)
    task = SessionStore().create_task("观测一轮")
    seed_turn(task["task_id"], "run-obs", "测试输入")
    usage = {"request_id": "req-1", "input_tokens": 11, "output_tokens": 7, "credits": 0.5}
    usage_again = dict(usage)
    install_sdk(
        monkeypatch,
        gateway,
        [
            SystemMessage("init", {"session_id": "session-1"}),
            AssistantMessage([TextBlock("先查")], "model", usage=usage, message_id="msg-1"),
            # 同一请求重发：按 message_id / request_id 去重，不产生第二条用量。
            AssistantMessage([TextBlock("先查")], "model", usage=usage_again, message_id="msg-1"),
            AssistantMessage(
                [TextBlock("结论")], "model", usage={"request_id": "req-2"}, message_id="msg-2"
            ),
            ResultMessage(
                "success",
                1200,
                800,
                False,
                3,
                "session-1",
                stop_reason="end_turn",
                result="完成",
                usage={"request_id": "req-2", "input_tokens": 9, "output_tokens": 4},
                total_credits=0.9,
                model_usage={"model": {"inputTokens": 20, "outputTokens": 11}},
            ),
        ],
    )

    events = asyncio.run(
        collect(
            gateway.stream_turn(
                observed_turn(
                    task["task_id"],
                    "run-obs",
                    materials=(
                        Material("本轮材料", "照常进行"),
                        # dict 材料按渲染文本量计：len(dict) 数的是键的个数，会记成 2。
                        Material("本轮触发", {"thread_id": "t-1", "message_id": "m-1"}),
                    ),
                )
            )
        )
    )
    assert [event["type"] for event in events][-1] == "done"

    row = summary("run-obs")
    materials = json.loads(row["materials"])
    # 字符数是渲染后的文本长度（含标题行）：字符串与 dict 材料同一把尺子。
    assert {"title": "本轮材料", "chars": len("## 本轮材料\n照常进行")} in materials["assembled"]
    rendered = "## 本轮触发\n" + json.dumps(
        {"thread_id": "t-1", "message_id": "m-1"}, ensure_ascii=False, indent=2
    )
    assert {"title": "本轮触发", "chars": len(rendered)} in materials["assembled"]
    sdk_result = json.loads(row["sdk_result"])
    assert sdk_result["duration_ms"] == 1200
    assert sdk_result["duration_api_ms"] == 800
    assert sdk_result["num_turns"] == 3
    assert sdk_result["stop_reason"] == "end_turn"
    assert [entry["request_id"] for entry in sdk_result["usage"]] == ["req-1", "req-2"]
    # 请求缺字段（req-2 只有标识）时合计留空，不拿部分值冒充总量。
    from server.sessions.observations import usage_totals

    assert usage_totals(sdk_result["usage"])["input_tokens"] is None

    # ResultMessage 的末次请求读数与会话累计快照一并落库，供读取面推算。
    assert sdk_result["result_usage"]["request_id"] == "req-2"
    assert sdk_result["result_usage"]["input_tokens"] == 9
    assert sdk_result["session_totals"] == {
        "input_tokens": 20,
        "output_tokens": 11,
        "credits": 0.9,
    }


def test_first_turn_records_context_before(settings, monkeypatch):
    """新会话首轮也必须有轮前读数：观测规格要求每轮两次读数，首轮不豁免。"""
    gateway = make_gateway(settings)
    task = SessionStore().create_task("首轮读数")
    seed_turn(task["task_id"], "run-first", "测试输入")
    install_sdk(monkeypatch, gateway, [result()])

    events = asyncio.run(
        collect(gateway.stream_turn(observed_turn(task["task_id"], "run-first")))
    )
    assert events[-1] == {"type": "done"}

    row = summary("run-first")
    before = json.loads(row["context_before"])
    after = json.loads(row["context_after"])
    assert before["used_percentage"] == 42.0
    assert after["used_percentage"] == 42.0


def test_cache_usage_is_recorded_without_inventing_missing_values(settings, monkeypatch):
    gateway = make_gateway(settings)
    task = SessionStore().create_task("缓存观测")
    seed_turn(task["task_id"], "run-cache", "测试输入")
    install_sdk(
        monkeypatch,
        gateway,
        [
            AssistantMessage(
                [TextBlock("完成")],
                "model",
                message_id="cached-message",
                usage={"request_id": "cached-request", "cache_read_input_tokens": 1200},
            ),
            ResultMessage(
                "success", 1, 1, False, 1, "session-1",
                usage={"cache_read_input_tokens": 1200, "cache_creation_input_tokens": 64},
                model_usage={
                    "model": {"cacheReadInputTokens": 2400, "cacheCreationInputTokens": 128}
                },
            ),
        ],
    )
    asyncio.run(collect(gateway.stream_turn(observed_turn(task["task_id"], "run-cache"))))
    observed = json.loads(summary("run-cache")["sdk_result"])
    assert observed["usage"][0]["cache_read_input_tokens"] == 1200
    assert "cache_creation_input_tokens" not in observed["usage"][0]
    assert observed["result_usage"]["cache_creation_input_tokens"] == 64
    assert observed["session_totals"]["cache_read_input_tokens"] == 2400


def test_resumed_turn_records_context_and_manual_compact(settings, monkeypatch):
    gateway = make_gateway(settings)
    task = SessionStore().create_task("压缩观测")
    seed_turn(task["task_id"], "run-compact", "继续任务")

    class CompactingClient:
        def __init__(self, _options):
            self.responses = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_context_usage(self):
            return {
                "contextWindow": {"usedPercentage": 85},
                "autoCompact": {"enabled": False, "thresholdPercentage": 80},
            }

        async def interrupt(self):
            raise AssertionError("测试脚本不应触发终止")

        async def query(self, message):
            if message == "/compact":
                self.responses = [
                    SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "manual"}}),
                    result(),
                ]
            else:
                self.responses = [result()]

        async def receive_response(self):
            for item in self.responses:
                yield item

    monkeypatch.setattr(agent_client, "QoderSDKClient", CompactingClient)
    turn = Turn(
        kind=TurnKind.MESSAGE,
        task_id=task["task_id"],
        sdk_session_id="session-1",
        message="继续任务",
        run_id="run-compact",
    )
    events = asyncio.run(collect(gateway.stream_turn(turn)))
    assert events[-1] == {"type": "done"}

    row = summary("run-compact")
    before = json.loads(row["context_before"])
    after = json.loads(row["context_after"])
    assert before["used_percentage"] == 85 and before["threshold_percentage"] == 80
    assert after["used_percentage"] == 85
    compact = steps("run-compact")[0]
    assert compact["kind"] == "compact" and compact["status"] == "ok"
    detail = json.loads(compact["detail"])
    assert detail["auto"] is False and detail["before"]["used_percentage"] == 85


def test_tool_steps_pair_with_timeline_and_distinguish_denial(settings, monkeypatch):
    gateway = make_gateway(settings)
    task = SessionStore().create_task("工具观测")
    seed_turn(task["task_id"], "run-tools", "查邮件")
    install_sdk(
        monkeypatch,
        gateway,
        [
            SystemMessage("init", {"session_id": "session-1"}),
            ToolCall("gmail_get_message", {"message_id": "msg_invite_001"}),
            result(),
        ],
    )
    from server.sessions.tool_trace import finish_tool_call

    hooks = tool_trace_hooks(task["task_id"], "run-tools", {"WebSearch", "WebFetch"})

    async def emit(event, name, call_id, **extra):
        await hooks[event][0].hooks[0](
            {
                "hook_event_name": event,
                "tool_name": name,
                "tool_use_id": call_id,
                "tool_input": {"query": call_id},
                **extra,
            },
            call_id,
            None,
        )

    asyncio.run(_scenario(gateway, task, emit, finish_tool_call))

    rows = steps("run-tools")
    tools = [row for row in rows if row["kind"] == "tool"]
    by_code = {row["code"]: row for row in tools}
    assert set(by_code) == {"gmail_get_message", "WebSearch", "WebFetch"}
    mcp_step = by_code["gmail_get_message"]
    assert mcp_step["status"] == "ok" and json.loads(mcp_step["detail"])["source"] == "mcp"
    assert mcp_step["item_id"] is not None
    assert by_code["WebFetch"]["status"] == "error"
    # 权限拒绝：无开始信号（started_at 为空），状态与执行失败区分。
    denied = by_code["WebSearch"]
    assert denied["status"] == "denied" and denied["started_at"] is None

    with session() as conn:
        items = {
            row["item_id"]: row
            for row in conn.execute(
                "SELECT item_id, tool_call_id FROM task_timeline_items WHERE kind = 'tool'"
            )
        }
    for step in tools:
        assert step["item_id"] in items
        assert items[step["item_id"]]["tool_call_id"] == step["tool_call_id"]


async def _scenario(gateway, task, emit, finish_tool_call):
    events = await collect(gateway.stream_turn(observed_turn(task["task_id"], "run-tools")))
    assert events[-1] == {"type": "done"}
    await emit("PreToolUse", "WebSearch", "web-1")
    await emit("PostToolUse", "WebSearch", "web-1", tool_response={"results": [1]})
    await emit("PostToolUseFailure", "WebFetch", "web-2", error="超时")
    await finish_tool_call(
        task_id=task["task_id"],
        run_id="run-tools",
        tool_call_id="web-3",
        name="WebSearch",
        arguments={},
        status="error",
        result="未授权的工具：WebSearch",
        source="builtin",
        denied=True,
    )


def test_catalog_failure_records_degraded_and_skipped(settings, monkeypatch):
    from server.agent.mcp import ToolServer
    from server.agent.toolset import ToolDeps
    from server.db import init_db
    from server.tools.gmail.service import MailDraftStore
    from server.tools.personal_kb.service import KbStore
    from tests.gateway.test_agent_stream import configured
    from tests.support.gmail_double import MockGmailClient

    init_db()
    kb = KbStore(settings.data_dir)
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(), tasks=SessionStore(), gmail=MockGmailClient(), kb_store=kb
        ),
        ToolServer(),
        settings=configured(settings),
    )
    task = SessionStore().create_task("降级观测")
    seed_turn(task["task_id"], "run-degraded", "查资料")

    def broken():
        raise RuntimeError("模拟读取失败")

    monkeypatch.setattr(kb, "catalog", broken)
    turn = observed_turn(task["task_id"], "run-degraded")
    observer = TurnObserver("run-degraded")
    gateway._options(
        turn,
        visible=[],
        path="/mnt/pebble/test",
        observer=observer,
    )

    degraded = steps("run-degraded")
    assert [row["code"] for row in degraded] == ["catalog_failed"]
    assert json.loads(degraded[0]["detail"]) == {"catalog": "kb"}
    row = summary("run-degraded")
    materials = json.loads(row["materials"])
    assert materials["skipped"] == [{"category": "资料目录", "reason": "生成失败"}]
