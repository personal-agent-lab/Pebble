import asyncio

from server.agent.mcp import ToolServer
from server.agent.toolset import TurnKind
from server.db import init_db, session, write
from server.gateway.agent_contract import Turn
from server.sessions.service import SessionStore
from server.skills import service
from server.skills.runtime import Scope, current
from server.skills.tools import skill_list, skill_read
from tests.gateway.test_agent_stream import (
    collect,
    injected_context,
    install_sdk,
    make_gateway,
    options_for,
    result,
)
from tests.support.mcp_http import mcp_session, tool_payload


def test_manual_body_and_automatic_catalog(settings):
    skill = service.create_skill(name="汇报", description="简洁汇报", body="先结论，再依据")
    gateway = make_gateway(settings)
    options = options_for(
        gateway,
        TurnKind.MESSAGE,
        skill_ids=[skill.id],
        skill_refs=({"id": skill.id, "revision": skill.content_hash},),
    )
    assert skill.body in injected_context(options)
    assert options.skills == []
    assert options.setting_sources == []
    options = options_for(gateway, TurnKind.MESSAGE, sdk_session_id="resume")
    assert skill.description in injected_context(options)
    assert skill.body not in injected_context(options)
    service.disable_skill(skill.id)
    options = options_for(gateway, TurnKind.MESSAGE, sdk_session_id="resume")
    assert options.hooks is None or skill.description not in injected_context(options)


def test_manual_skill_is_announced_when_loaded(settings, monkeypatch):
    skill = service.create_skill(name="汇报", description="简洁汇报", body="先结论，再依据")
    gateway = make_gateway(settings)
    install_sdk(monkeypatch, gateway, [result()])
    turn = Turn(
        kind=TurnKind.MESSAGE,
        task_id="task-1",
        sdk_session_id=None,
        message="写一份汇报",
        skill_refs=({"id": skill.id, "revision": skill.content_hash},),
    )
    events = asyncio.run(collect(gateway.stream_turn(turn)))
    assert events == [
        {"type": "activity", "text": "已加载「汇报」Skill（用户选择）"},
        {"type": "done"},
    ]


def test_mcp_scope_and_actual_usage(settings):
    init_db()
    skill = service.create_skill(name="汇报", description="简洁汇报", body="先结论，再依据")
    task = SessionStore().create_task("测试")["task_id"]
    with session() as conn, write(conn):
        conn.execute(
            "INSERT INTO agent_runs(run_id,task_id,kind,input,status,created_at) "
            "VALUES ('run',?,'message','{}','running','now')",
            (task,),
        )

    async def scenario():
        server = ToolServer()
        definitions = [skill_read, skill_list]
        scope = Scope("run", {skill.id}, True, set())
        token = current.set(scope)
        try:
            async with server.serve(definitions, task_id=task, queued=asyncio.Queue()) as path:
                # 模拟 HTTP 请求运行在不同上下文。
                current.set(None)
                async with mcp_session(server, f"http://test{path}") as client:
                    result = await client.call_tool("skill_read", {"skill_id": skill.id})
                    assert result.isError
                    scope.excluded.clear()
                    result = await client.call_tool("skill_read", {"skill_id": skill.id})
                    assert not result.isError
                    assert tool_payload(result)["body"] == skill.body
        finally:
            current.reset(token)

    asyncio.run(scenario())
    with session() as conn:
        row = conn.execute("SELECT * FROM skill_run_links").fetchone()
        assert row["revision"] == skill.content_hash
        assert row["source"] == "auto"
    with session() as conn, write(conn):
        conn.execute("UPDATE agent_runs SET status='done', finished_at='now'")
    SessionStore().delete_task(task)
