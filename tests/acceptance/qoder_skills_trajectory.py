"""真实 Qoder 模型的执行轨迹验收（spec §12 场景 7 的轨迹一半，技能阶段 C）。

显式运行，不进入 pytest；使用临时实例目录，不碰 `.data`。依次验证：

1. 失败链重建：真实对话里模型按要求先查看一个不存在的技能（`unknown_skill`
   失败）、调整后读取真实技能成功并照做；轨迹按序记下用户消息 → 失败调用 →
   成功调用 → 回答，参数含值、成败与返回内容可查（契约 §3）。
2. 重启持久：重新初始化并重开数据库（等价服务重启后的路径）轨迹仍在，
   已完成轮在轨迹视图里映射为 completed。
3. 依据校验：轨迹条目可作为边界内依据条目，编造的条目不算。
4. 时间线载荷：任务页读到的条目里工具行字段完整（前端折叠行的数据源）。

运行：`uv run --project server python -m tests.acceptance.qoder_skills_trajectory`
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI

from server.agent.client import QoderGateway
from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.agent.toolset import ToolDeps
from server.config import Settings, get_settings
from server.db import init_db, session
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.sessions.timeline import TimelineStore
from server.sessions.trajectory import item_within_boundary, trajectory
from server.skills.models import ChangeAction, ChangeActor
from server.skills.service import ChangeRequest, SkillService
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient

WEEKLY_BODY = """## 步骤

1. 用一句话说明本周重点。
2. 最后一行固定写「——周报结束——」，不写别的内容。
"""

TURN_TIMEOUT = 240.0


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check(condition: bool, message: str, evidence: object) -> None:
    if not condition:
        raise AssertionError(f"{message}：证据={evidence!r}")


async def wait_run_done(service: GatewayRuntime, run_id: str) -> dict:
    deadline = time.monotonic() + TURN_TIMEOUT
    while time.monotonic() < deadline:
        row = service.get_run(run_id)
        if row["status"] in ("done", "error", "interrupted"):
            return row
        await asyncio.sleep(0.5)
    raise AssertionError(f"等待轮次结束超时：{run_id}")


def tool_view(conn, task_id: str) -> dict:
    return trajectory(conn, task_id)


async def scenario_failure_chain(service: GatewayRuntime, skills: SkillService) -> dict:
    """场景 1：先失败后成功的调用链完整落在轨迹里。"""

    created = skills.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload={
                "skill_id": "weekly-report",
                "name": "周报整理",
                "description": "按固定格式整理本周一句话周报",
                "body": WEEKLY_BODY,
            },
            actor=ChangeActor.USER,
            reason="验收脚本：管理页创建",
        )
    )
    check(created["status"] == "applied", "手工技能没有直接生效", created)

    started = service.start_task(
        model="auto",
        message=(
            "请用技能工具做两件事：先查看技能 no-such-skill 的正文"
            "（这个技能不存在，看一下工具返回什么），再查看 weekly-report 的正文，"
            "然后严格按它的步骤整理一句话周报。本周重点：发布定在 9 月 30 日。"
        ),
        attachments=[],
    )
    task_id = started["task"]["task_id"]
    run = await wait_run_done(service, started["run"]["run_id"])
    check(run["status"] == "done", "轮次没有正常结束", run)

    with session() as conn:
        view = tool_view(conn, task_id)
    entries = view["entries"]
    kinds = [(entry["kind"], entry["role"]) for entry in entries]
    check(
        kinds[0] == ("text", "user"),
        "轨迹第一条不是用户消息",
        kinds,
    )
    tools = [entry for entry in entries if entry["kind"] == "tool"]
    check(len(tools) >= 2, "轨迹里的工具调用不足两次", kinds)

    failed = [
        entry
        for entry in tools
        if entry["status"] == "error"
        and entry["name"] == "skill_view"
        and entry["arguments"].get("skill_id") == "no-such-skill"
    ]
    succeeded = [
        entry
        for entry in tools
        if entry["status"] == "ok"
        and entry["name"] == "skill_view"
        and entry["arguments"].get("skill_id") == "weekly-report"
    ]
    check(len(failed) == 1, "没有恰好一次对不存在技能的失败调用", tools)
    check(len(succeeded) >= 1, "没有成功读取真实技能的调用", tools)
    check("unknown_skill" in (failed[0]["content"] or ""), "失败调用的返回没有落进轨迹", failed[0])
    check(
        len({entry["tool_call_id"] for entry in tools}) == len(tools),
        "tool_call_id 没有逐次唯一",
        [entry["tool_call_id"] for entry in tools],
    )

    answer = entries[-1]
    check(answer["kind"] == "text" and answer["role"] == "assistant", "轨迹最后一条不是回答", kinds)
    positions = {
        "user": entries[0]["sequence"],
        "failed": failed[0]["sequence"],
        "ok": succeeded[0]["sequence"],
        "answer": answer["sequence"],
    }
    check(
        positions["user"] < positions["failed"] < positions["ok"] < positions["answer"],
        "轨迹没有按 用户→失败→成功→回答 排序",
        positions,
    )
    check(
        view["turns"] and all(turn["status"] == "completed" for turn in view["turns"]),
        "已完成轮在轨迹视图里不是 completed",
        view["turns"],
    )

    # 回答确实照技能执行，说明成功调用读到的正文进入了上下文。
    timeline_items = TimelineStore().list_items(task_id)["items"]
    check("——周报结束——" in timeline_items[-1]["text"], "回答没有遵循技能格式", timeline_items[-1])

    # 依据校验（契约 §5 的存在性由 skills 服务做，边界归属由轨迹视图做）。
    through = entries[-1]["item_id"]
    with session() as conn:
        check(
            item_within_boundary(conn, task_id, succeeded[0]["item_id"], through),
            "成功调用的条目不在边界内",
            {"item": succeeded[0]["item_id"], "through": through},
        )
        check(
            not item_within_boundary(conn, task_id, str(uuid4()), through),
            "编造的条目被认作依据",
            through,
        )

    # 时间线载荷：前端折叠行的数据源字段完整。
    payload_tools = [item for item in timeline_items if item["kind"] == "tool"]
    check(
        any(
            item["name"] == "skill_view"
            and item["arguments"] == {"skill_id": "no-such-skill"}
            and item["status"] == "error"
            and "unknown_skill" in item["result"]
            for item in payload_tools
        ),
        "时间线载荷缺少完整的工具条目字段",
        payload_tools,
    )
    return {
        "kinds": kinds,
        "positions": positions,
        "failed_result": failed[0]["content"],
        "turns": view["turns"],
        "answer": timeline_items[-1]["text"],
    }


def scenario_reopen(task_id: str) -> dict:
    """场景 2：重新初始化并重开数据库后轨迹仍在。"""

    check(init_db() == 21, "重新初始化没有停在 v21", init_db())
    with session() as conn:
        reopened = tool_view(conn, task_id)
    check(
        len(reopened["entries"]) > 0 and reopened["turns"],
        "重开后轨迹丢失",
        reopened,
    )
    check(
        all(turn["status"] == "completed" for turn in reopened["turns"]),
        "重开后轮状态映射失效",
        reopened["turns"],
    )
    return {"entries": len(reopened["entries"]), "turns": reopened["turns"]}


async def verify(root: Path) -> dict:
    if get_settings().qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    settings = Settings(data_dir=root, tool_port=available_port())
    init_db(settings.db_path)
    skills = SkillService(root, settings.db_path)
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(settings.db_path),
            tasks=SessionStore(settings.db_path),
            gmail=MockGmailClient(),
            skills=skills,
        ),
        ToolServer(),
        settings=settings,
    )
    service = GatewayRuntime(gateway)
    app = FastAPI()
    app.mount(MCP_MOUNT_PATH, gateway.tool_server)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=settings.tool_port, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.01)
    try:
        chain = await scenario_failure_chain(service, skills)
        task_id = next(row["task_id"] for row in SessionStore(settings.db_path).list_tasks())
        reopened = scenario_reopen(task_id)
        return {"failure_chain": chain, "after_reopen": reopened}
    finally:
        server.should_exit = True
        await serving
        await service.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-trajectory-") as directory:
        root = Path(directory)
        os.environ["PEBBLE_DATA_DIR"] = str(root)
        get_settings.cache_clear()
        report = asyncio.run(verify(root))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
