"""执行轨迹：mcp 边界把每次工具调用（含被拒与失败）记入任务时间线。

契约 contracts/skill.md §3：条目按任务内 sequence 排序、参数含值、成败可查，
可重建“调用 → 失败 → 调整 → 成功 → 回答”；一次性会话（run_id 为空）不记。
"""

import asyncio
import json

from server.agent.client import tool_trace_hooks
from server.agent.mcp import ToolServer
from server.agent.toolset import ALLOWED_EFFECTS, ToolDeps, TurnKind, build_tools, exposed_tools
from server.db import init_db, session, write
from server.sessions import runs
from server.sessions.service import SessionStore, timestamp
from server.sessions.timeline import TimelineStore, insert_text, insert_tool_item
from server.sessions.tool_trace import begin_tool_call, finish_tool_call
from server.sessions.trajectory import item_within_boundary, trajectory
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore
from tests.support.gmail_double import MockGmailClient
from tests.support.mcp_http import mcp_session, tool_payload


def build(settings):
    init_db()
    tasks = SessionStore()
    tools = build_tools(
        ToolDeps(
            drafts=MailDraftStore(),
            tasks=tasks,
            gmail=MockGmailClient(),
            kb_store=KbStore(settings.data_dir),
        )
    )
    return tools, tasks


def seed_turn(task_id: str, run_id: str, message: str) -> None:
    """登记一条已开始的轮次与它的用户消息，轨迹条目挂在其上。"""
    with session() as conn, write(conn):
        runs.insert(
            conn, run_id, task_id, runs.KIND_MESSAGE, {"message": message}, None, timestamp()
        )
        conn.execute(
            "UPDATE agent_runs SET status = 'running', started_at = ? WHERE run_id = ?",
            (timestamp(), run_id),
        )
        insert_text(conn, task_id, run_id, "user", message)


def tool_items(task_id: str) -> list[dict]:
    with session() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_timeline_items WHERE task_id = ? AND kind = 'tool' "
                "ORDER BY sequence",
                (task_id,),
            )
        ]


def test_tool_calls_recorded_with_values_status_and_order(settings):
    tools, tasks = build(settings)
    task = tasks.create_task("查一封邮件")
    seed_turn(task["task_id"], "run-1", "帮我看看邀请邮件")

    server = ToolServer()
    visible = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])
    seen_states: list[str] = []

    def timeline_changed() -> None:
        rows = tool_items(task["task_id"])
        seen_states.append(rows[-1]["tool_status"])

    async def scenario():
        async with (
            server.serve(
                visible,
                task_id=task["task_id"],
                queued=asyncio.Queue(),
                run_id="run-1",
                on_timeline_change=timeline_changed,
            ) as path,
            mcp_session(server, f"http://127.0.0.1:8000{path}") as mcp,
        ):
            ok = await mcp.call_tool("gmail_get_message", {"message_id": "msg_invite_001"})
            assert ok.isError is False

            missing = await mcp.call_tool("gmail_get_message", {"message_id": "msg_missing"})
            assert missing.isError is True

            rejected = await mcp.call_tool("no_such_tool", {"whatever": "x"})
            assert rejected.isError is True
            assert tool_payload(rejected)["error"] == "unknown_tool"

    asyncio.run(scenario())

    assert seen_states == ["running", "ok", "running", "error", "running", "error"]

    rows = tool_items(task["task_id"])
    assert [row["tool_name"] for row in rows] == [
        "gmail_get_message",
        "gmail_get_message",
        "no_such_tool",
    ]
    # 参数含值；注入的 task_id 不混进记录。
    assert json.loads(rows[0]["tool_arguments"]) == {"message_id": "msg_invite_001"}
    assert [row["tool_status"] for row in rows] == ["ok", "error", "error"]
    assert "msg_invite_001" in rows[0]["tool_result"]
    assert len({row["tool_call_id"] for row in rows}) == 3
    # 轨迹与既有条目共用任务内序号：用户消息在最前。
    with session() as conn:
        order = [
            (row["kind"], row["role"])
            for row in conn.execute(
                "SELECT kind, role FROM task_timeline_items WHERE task_id = ? ORDER BY sequence",
                (task["task_id"],),
            )
        ]
    assert order[0] == ("text", "user")
    assert order.count(("tool", None)) == 3

    # 时间线载荷与轨迹视图给出契约 §3 的形状。
    items = TimelineStore().list_items(task["task_id"])["items"]
    tool_payloads = [item for item in items if item["kind"] == "tool"]
    assert tool_payloads[0]["arguments"] == {"message_id": "msg_invite_001"}
    assert tool_payloads[1]["status"] == "error"

    with session() as conn:
        view = trajectory(conn, task["task_id"])
        entries = [entry for entry in view["entries"] if entry["kind"] == "tool"]
        assert [entry["role"] for entry in entries] == ["tool", "tool", "tool"]
        assert [entry["status"] for entry in entries] == ["ok", "error", "error"]
        assert entries[0]["arguments"] == {"message_id": "msg_invite_001"}
        assert view["turns"][0]["status"] == "running"
        # 依据条目校验：真实条目在边界内，编造的条目不算。
        through = items[-1]["item_id"]
        assert item_within_boundary(conn, task["task_id"], entries[0]["item_id"], through)
        assert not item_within_boundary(conn, task["task_id"], "made-up", through)


def test_onshot_sessions_without_run_id_are_not_recorded(settings):
    tools, tasks = build(settings)
    task = tasks.create_task("一次性会话")
    server = ToolServer()
    visible = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.NEW_MAIL])

    async def scenario():
        async with (
            server.serve(visible, task_id=task["task_id"], queued=asyncio.Queue()) as path,
            mcp_session(server, f"http://127.0.0.1:8000{path}") as mcp,
        ):
            ok = await mcp.call_tool("gmail_get_message", {"message_id": "msg_invite_001"})
            assert ok.isError is False

    asyncio.run(scenario())
    assert tool_items(task["task_id"]) == []


def test_trajectory_survives_reopen(settings):
    tools, tasks = build(settings)
    task = tasks.create_task("重启后可查")
    seed_turn(task["task_id"], "run-keep", "查邮件")

    server = ToolServer()
    visible = exposed_tools(tools, allowed=ALLOWED_EFFECTS[TurnKind.MESSAGE])

    async def scenario():
        async with (
            server.serve(
                visible, task_id=task["task_id"], queued=asyncio.Queue(), run_id="run-keep"
            ) as path,
            mcp_session(server, f"http://127.0.0.1:8000{path}") as mcp,
        ):
            await mcp.call_tool("gmail_get_message", {"message_id": "msg_invite_001"})

    asyncio.run(scenario())
    assert len(tool_items(task["task_id"])) == 1

    # 重新初始化（等价于服务重启后的打开路径）不丢轨迹、不重复迁移。
    assert init_db() == 21
    rows = tool_items(task["task_id"])
    assert len(rows) == 1
    assert rows[0]["tool_status"] == "ok"


def test_builtin_callbacks_pair_parallel_calls_and_leave_interrupted_call(settings):
    build(settings)
    task = SessionStore().create_task("并行查询")
    task_id = task["task_id"]
    seed_turn(task_id, "run-hooks", "查询两处资料")
    hooks = tool_trace_hooks(task_id, "run-hooks", {"WebSearch", "WebFetch"})

    async def emit(event, name, call_id, **extra):
        callback = hooks[event][0].hooks[0]
        await callback(
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

    async def scenario():
        await emit("PreToolUse", "WebSearch", "first")
        await emit("PreToolUse", "WebSearch", "second")
        await emit("PreToolUse", "WebFetch", "unfinished")
        await emit("PreToolUse", "mcp__pebble__skill_view", "mcp")
        await emit("PostToolUse", "WebSearch", "second", tool_response={"results": [2]})
        await emit("PostToolUse", "WebSearch", "first", tool_response={"results": [1]})
        await emit("PostToolUse", "WebSearch", "first", tool_response={"results": [99]})

    asyncio.run(scenario())
    rows = tool_items(task_id)
    assert [row["tool_call_id"] for row in rows] == ["first", "second", "unfinished"]
    assert [row["tool_status"] for row in rows] == ["ok", "ok", "running"]
    assert json.loads(rows[0]["tool_result"]) == {"results": [1]}
    assert rows[2]["tool_result"] is None
    assert init_db() == 21
    assert [row["tool_call_id"] for row in tool_items(task_id)] == ["first", "second", "unfinished"]


def test_tool_start_order_survives_out_of_order_finish(settings):
    build(settings)
    task = SessionStore().create_task("并行工具")
    task_id = task["task_id"]
    seed_turn(task_id, "run-order", "执行两个工具")

    async def scenario():
        for call_id in ("slow", "fast"):
            await begin_tool_call(
                task_id=task_id,
                run_id="run-order",
                tool_call_id=call_id,
                name="skill_view",
                arguments={"skill_id": call_id},
            )
        for call_id in ("fast", "slow"):
            await finish_tool_call(
                task_id=task_id,
                run_id="run-order",
                tool_call_id=call_id,
                name="skill_view",
                arguments={"skill_id": call_id},
                status="ok",
                result=call_id,
            )

    asyncio.run(scenario())
    assert [row["tool_call_id"] for row in tool_items(task_id)] == ["slow", "fast"]


def test_retry_keeps_tool_items_and_drops_partial_answer(settings):
    tools, tasks = build(settings)
    task = tasks.create_task("重试中断消息")
    run_id = "run-retry-keep"
    seed_turn(task["task_id"], run_id, "原问题")
    with session() as conn, write(conn):
        insert_text(conn, task["task_id"], run_id, "assistant", "半截回答")
        insert_tool_item(
            conn,
            task_id=task["task_id"],
            run_id=run_id,
            tool_call_id="tc-1",
            name="gmail_get_message",
            arguments={"message_id": "msg_invite_001"},
            status="ok",
            result='{"subject": "邀请"}',
            created_at=timestamp(),
        )
        runs.finish(conn, run_id, "interrupted", "进程退出", timestamp())

    with session() as conn, write(conn):
        runs.retry_latest_message(conn, task["task_id"])

    with session() as conn:
        remaining = [
            (row["kind"], row["role"])
            for row in conn.execute(
                "SELECT kind, role FROM task_timeline_items WHERE task_id = ? ORDER BY sequence",
                (task["task_id"],),
            )
        ]
    # 半截回答被替换；用户消息与真实发生的调用保留。
    assert remaining == [("text", "user"), ("tool", None)]


def test_trajectory_boundaries_slice_by_item(settings):
    tools, tasks = build(settings)
    task = tasks.create_task("边界截取")
    seed_turn(task["task_id"], "run-a", "第一轮")
    seed_turn(task["task_id"], "run-b", "第二轮")
    with session() as conn:
        view = trajectory(conn, task["task_id"])
        assert [entry["turn_id"] for entry in view["entries"]] == ["run-a", "run-b"]
        first, second = view["entries"]
        sliced = trajectory(
            conn, task["task_id"], since_item_id=first["item_id"], through_item_id=second["item_id"]
        )
        assert [entry["item_id"] for entry in sliced["entries"]] == [second["item_id"]]
