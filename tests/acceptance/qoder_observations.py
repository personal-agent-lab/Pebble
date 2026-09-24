"""真实 Qoder 运行观测验收（spec §4 第 1、3、4 项）。

显式运行，不进入 pytest；使用临时实例目录，不碰 `.data`。真实 CN SDK 子进程经真实
GatewayRuntime 调度，工具走进程内的 MCP 端点与内置联网工具，观测按生产路径落库：

1. 一轮含 Pebble MCP 工具（`kb_search`）与内置 `WebSearch` 的调用：轨迹条目与观测步骤
   各恰好一条、状态／起止／item_id 互相对应，用量条目有唯一请求标识，轮次时长与
   上下文占用有值；指示模型读取工作区外的文件制造权限拒绝（尽力项）。
2. 大材料把上下文推过压缩阈值：核对压缩步骤的前后占用，没有边界信号就不声称压缩。
3. 重开数据库核对数据不变；删任务后观测记录随 agent_runs 级联消失；观测数据里搜不到
   材料正文、工具参数与端点的内容。

运行：`uv run --project server python -m tests.acceptance.qoder_observations`
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from server.agent.client import QoderGateway
from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.agent.toolset import ToolDeps
from server.config import Settings, get_settings
from server.db import SCHEMA_VERSION, init_db, session
from server.gateway.runtime import GatewayRuntime
from server.memory.service import MemoryStore
from server.sessions.observations import read_observations, usage_totals
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore
from tests.support import seed_memory
from tests.support.gmail_double import MockGmailClient

# 资料里的编号：模型必须经 kb_search / kb_read 才能核对，目录里看不到正文。
KB_CODE = "NEBULA-3390"
KB_BODY = f"## 交付约定\n二期交付标识为 {KB_CODE}，负责人是周恺，验收窗口是十月的第二周。\n"
# 工作区之外的文件：模型若照要求去读，必然经过权限回调。
OUTSIDE_FILE = Path("/etc/hosts")
OUTSIDE_HINT = "hosts"
# 压缩场景的填充输入：真实往上下文里推，不用假的用量数字。实测 9000 行约占窗口七成，
# 这里留出余量越过阈值；压缩是否发生仍以 SDK 的边界信号为准。
FILLER_LINES = 16_000
# 验收固定的托管型号：不写 auto，避免 Auto 路由到当天额度用尽的型号，验收结果也就与
# 具体型号绑定、可比较。`auto` 仍是产品默认值，这里只是把验收变量固定下来。
MODEL = os.environ.get("PEBBLE_ACCEPTANCE_MODEL", "qmodel_38max")

TURN_TIMEOUT = 420.0


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check(condition: bool, message: str, evidence: object) -> None:
    if not condition:
        raise AssertionError(f"{message}：证据={evidence!r}")


async def wait_run_done(service: GatewayRuntime, run_id: str) -> dict:
    """等这一轮到终态；同时等后台的记忆判断与标题任务收尾，观测才算写完。"""
    deadline = time.monotonic() + TURN_TIMEOUT
    while time.monotonic() < deadline:
        row = service.get_run(run_id)
        if row["status"] in ("done", "error", "interrupted"):
            pending = [*service._active.values(), *service._titles, *service._judges]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return service.get_run(run_id)
        await asyncio.sleep(0.5)
    raise AssertionError(f"等待轮次结束超时：{run_id}")


# 账号模型服务在本机偶发不可用：这类失败与观测无关，重做一次再下结论，报告里不隐藏。
TRANSIENT_ERRORS = ("模型当前不可用", "模型目录读取失败", "模型调用失败", "SDK 调用未给出结束事件")
# 额度用尽不是偶发故障：重试没有意义，直接停下并说明，避免把账号额度耗在重试上。
EXHAUSTED_ERRORS = ("credit usage limit", "pricingUrl", "额度")
RETRY_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 30.0


def exhausted(run: dict) -> bool:
    return any(marker in (run["error"] or "") for marker in EXHAUSTED_ERRORS)


def transient(run: dict) -> bool:
    return any(marker in (run["error"] or "") for marker in TRANSIENT_ERRORS)


async def transients_retried(action, describe: str) -> dict:
    """执行一次真实轮次动作并等到终态；账号模型服务偶发不可用时重做，其余结果原样返回。"""
    failed = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        failed = await action()
        if failed["status"] == "done" or not transient(failed):
            return failed
        if exhausted(failed):
            raise RuntimeError(
                f"账号额度已用尽，本脚本无法继续真实调用（{failed['error']}）；"
                "恢复额度后重新运行即可。"
            )
        print(f"{describe}：模型服务偶发不可用（{failed['error']}）")
        if attempt < RETRY_ATTEMPTS:
            print(f"{RETRY_WAIT_SECONDS:.0f} 秒后重做（第 {attempt} 次）")
            await asyncio.sleep(RETRY_WAIT_SECONDS)
    return failed


async def start_turn(runtime: GatewayRuntime, task_id: str, message: str) -> dict:
    """提交一轮并等到终态。"""

    async def action() -> dict:
        row = runtime.submit_message(task_id, message)
        return await wait_run_done(runtime, row["run_id"])

    return await transients_retried(action, "继续轮")


async def start_task(
    runtime: GatewayRuntime, instance: Instance, message: str, *, ready: bool = False
) -> tuple[str, dict]:
    """建任务并跑首轮；首轮遇到偶发失败时用同一任务重提一轮。

    `ready` 为真时先备好任务工作区的 `attachments` 目录：本轮要暴露内置 Read 就得先有它。
    """
    started = runtime.start_task(model=MODEL, message=message, attachments=[])
    task_id = started["task"]["task_id"]
    if ready:
        (instance.root / "agent" / "workspaces" / task_id / "attachments").mkdir(
            parents=True, exist_ok=True
        )

    async def first() -> dict:
        return await wait_run_done(runtime, started["run"]["run_id"])

    run = await first()
    if exhausted(run):
        raise RuntimeError(
            f"账号额度已用尽，本脚本无法继续真实调用（{run['error']}）；恢复额度后重新运行即可。"
        )
    if run["status"] != "done" and transient(run):
        print(f"首轮模型服务偶发不可用（{run['error']}），用同一任务重提一轮")
        run = await start_turn(runtime, task_id, message)
    return task_id, run


class FixedCatalog:
    """固定型号目录：本脚本只关心观测，装配期的目录链路用固定值顶替。"""

    def __init__(self, model: str) -> None:
        self.model = model

    async def validate(self, model: str, *, new_task: bool = True) -> str:
        return model

    async def close(self) -> None:
        return None


class Instance:
    """临时实例：真实网关 + 真实 SDK + 真实资料库／记忆／数据库。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = root / "pebble.db"
        self.port = available_port()
        init_db(self.database)
        self.kb = KbStore(root)
        self.memory = MemoryStore(root)
        self.tasks = SessionStore(self.database)
        self.tool_server = ToolServer()
        settings = Settings(data_dir=root, tool_port=self.port)
        self.gateway = QoderGateway(
            ToolDeps(
                drafts=MailDraftStore(self.database),
                tasks=self.tasks,
                gmail=MockGmailClient(),
                kb_store=self.kb,
                memory_store=self.memory,
            ),
            self.tool_server,
            settings=settings,
        )
        # 记忆与数据库都指向临时目录：验收只写临时数据，不碰实例 .data。型号目录用固定目录
        # 顶替：本脚本验收的是观测，账号目录是另一条链路，它偶发读取失败会让每一轮在装配期
        # 就失败（"模型当前不可用"），与观测无关，且验收里没有必要为它多起一次 CLI 目录读取。
        self.runtime = GatewayRuntime(
            self.gateway,
            memory_store=self.memory,
            model_catalog=FixedCatalog(MODEL),
            path=self.database,
        )
        app = FastAPI()
        app.mount(MCP_MOUNT_PATH, self.tool_server)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.serving: asyncio.Task | None = None

    async def start(self) -> None:
        self.serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.serving.done():
                await self.serving
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        await self.runtime.close()
        self.server.should_exit = True
        if self.serving is not None:
            await self.serving


def timeline_tools(task_id: str) -> list[dict]:
    with session() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT item_id, run_id, tool_call_id, tool_name AS name, "
                "tool_status AS status FROM task_timeline_items "
                "WHERE task_id = ? AND kind = 'tool' ORDER BY rowid",
                (task_id,),
            )
        ]


def observation_dump(task_id: str) -> list[dict]:
    return read_observations(task_id)


def step_rows(task_id: str) -> list[dict]:
    with session() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT s.* FROM observation_steps s JOIN agent_runs r ON r.run_id = s.run_id "
                "WHERE r.task_id = ? ORDER BY s.rowid",
                (task_id,),
            )
        ]


async def scenario_tools(instance: Instance, runtime: GatewayRuntime) -> dict:
    """场景 1：MCP 工具 + 内置联网工具的调用与观测逐条对应；权限拒绝是尽力项。

    任务工作区里先建好 `attachments` 目录：只有存在该目录时本轮才向模型开放内置 Read，
    `_options` 的 read_enabled 就是按这个目录算的。开放后才谈得上“实际到达权限回调的拒绝”。
    """

    instance.kb.save(
        title="星云二期交付约定",
        body=KB_BODY,
        path="项目/星云/交付.md",
        summary="二期交付标识与验收窗口",
    )
    seed_memory(instance.memory, "user", "用户希望被称为林舟。")
    task_id, run = await start_task(
        runtime,
        instance,
        (
            "现在做三件事，每件都要真的调用工具，不要凭印象回答：\n"
            f"1. 用 kb_search 检索「{KB_CODE}」，再用 kb_read 读取命中的资料原文，"
            "核对二期交付标识与验收窗口。\n"
            "2. 用 WebSearch 搜索「Qoder Agent SDK」，一句话说明它能做什么。\n"
            f"3. 用 Read 读取 {OUTSIDE_FILE}，告诉我第一行是什么；读不到就说明工具怎么回复的。\n"
            "最后用三句话分别汇报三件事的结果。"
        ),
        ready=True,
    )
    check(run["status"] == "done", "轮次没有正常结束", run)

    observed = [item for item in observation_dump(task_id) if item["run_id"] == run["run_id"]]
    check(len(observed) == 1, "本轮没有唯一的观测记录", observed)
    observed = observed[0]
    check(observed["status"] == "done", "观测里的轮次状态不是 done", observed["status"])
    check(isinstance(observed["sdk_result"], dict), "轮次摘要缺少 SDK 结果", observed["sdk_result"])
    sdk_result = observed["sdk_result"]
    check(
        isinstance(sdk_result["duration_ms"], int) and sdk_result["duration_ms"] > 0,
        "SDK 总时长没有落库",
        sdk_result,
    )
    check(
        isinstance(sdk_result["duration_api_ms"], int) and sdk_result["duration_api_ms"] > 0,
        "SDK API 时长没有落库",
        sdk_result,
    )
    check(sdk_result["is_error"] is False, "正常轮次被记成错误", sdk_result)

    materials = observed["materials"]
    check(isinstance(materials, dict), "轮次摘要缺少材料", materials)
    titles = [item["title"] for item in materials["assembled"]]
    check("关于你" in titles, "记忆材料没有进摘要", titles)
    check("资料目录" in titles, "资料目录没有进摘要", titles)
    check(
        all(
            isinstance(item["chars"], int) and item["chars"] > 0 for item in materials["assembled"]
        ),
        "材料字符数缺失",
        materials["assembled"],
    )
    check(materials["skipped"] == [], "本轮不该有跳过的材料", materials["skipped"])

    check(
        isinstance(observed["context_after"], dict)
        and observed["context_after"]["used_percentage"] is not None,
        "轮末上下文占用没有落库",
        observed["context_after"],
    )

    tools = timeline_tools(task_id)
    steps = [step for step in observed["steps"] if step["kind"] == "tool"]
    check(len(steps) == len(tools), "工具步骤与时间线工具条目数量不一致", (steps, tools))
    by_call = {step["tool_call_id"]: step for step in steps}
    check(len(by_call) == len(steps), "工具步骤的 tool_call_id 没有逐条唯一", steps)
    for item in tools:
        step = by_call.get(item["tool_call_id"])
        check(step is not None, f"轨迹条目 {item['name']} 没有对应观测步骤", steps)
        check(
            step["item_id"] == item["item_id"],
            f"{item['name']} 的 item_id 与轨迹不对应",
            (step["item_id"], item["item_id"]),
        )
        expected_status = "error" if step["status"] == "denied" else step["status"]
        check(
            item["status"] == expected_status,
            f"{item['name']} 的步骤状态与轨迹状态不一致",
            (step["status"], item["status"]),
        )
        check(
            isinstance(step["detail"], dict) and step["detail"].get("source") in ("mcp", "builtin"),
            f"{item['name']} 的步骤没有来源",
            step["detail"],
        )
    for step in steps:
        check(step["status"] != "running", "步骤停在 running", step)
        check(step["ended_at"] is not None, "已结束的步骤没有结束时间", step)
        if step["status"] == "denied":
            # 实测：真实拒绝先到 PreToolUse、后到权限回调，因此 started_at 可能有值；
            # 权限回调先到时它为空。两种形状都是合法的，只要求顺序不颠倒。
            if step["started_at"] is not None:
                check(step["started_at"] <= step["ended_at"], "拒绝步骤的起止时间顺序颠倒", step)
        else:
            check(step["started_at"] is not None, "已执行的步骤没有开始时间", step)
            check(step["started_at"] <= step["ended_at"], "步骤的起止时间顺序颠倒", step)

    names = {item["name"] for item in tools}
    check("kb_search" in names, "模型没有调用 kb_search", tools)
    searches = [step for step in steps if step["code"] == "kb_search" and step["status"] == "ok"]
    check(len(searches) >= 1, "kb_search 没有成功过一次", steps)
    check(
        searches[0]["detail"]["source"] == "mcp",
        "MCP 调用的来源记错",
        searches[0],
    )

    builtin = [item for item in tools if item["name"] in ("WebSearch", "WebFetch", "Read")]
    check(builtin, "内置工具没有任何调用记录", tools)
    usage = sdk_result["usage"]
    check(isinstance(usage, list) and usage, "没有收到任何请求级用量", sdk_result)
    identifiers = [entry.get("request_id") or entry.get("message_id") for entry in usage]
    check(all(isinstance(value, str) and value for value in identifiers), "用量条目缺标识", usage)
    check(
        len(set(identifiers)) == len(identifiers),
        "同一轮的用量条目重名，无法排除重复",
        identifiers,
    )
    detected = {
        "assistant_message_ids": [entry["message_id"] for entry in usage],
        "request_ids": [entry["request_id"] for entry in usage],
        "num_turns": sdk_result["num_turns"],
        "usage_entries": usage,
        "usage_totals": usage_totals(usage),
    }
    denied_report = denial_report(by_call, tools)
    return {
        "task_id": task_id,
        "run_id": run["run_id"],
        "timeline_tools": [
            {"name": item["name"], "status": item["status"], "item_id": item["item_id"]}
            for item in tools
        ],
        "steps": [
            {
                "code": step["code"],
                "status": step["status"],
                "source": step["detail"].get("source"),
                "chars": step["detail"].get("chars"),
                "has_started_at": step["started_at"] is not None,
            }
            for step in steps
        ],
        "materials": materials,
        "sdk_result": {
            "duration_ms": sdk_result["duration_ms"],
            "duration_api_ms": sdk_result["duration_api_ms"],
            "num_turns": sdk_result["num_turns"],
        },
        "usage": detected,
        "context_before": observed["context_before"],
        "context_after": observed["context_after"],
        "denial": denied_report,
    }


def denial_report(by_call: dict, tools: list[dict]) -> dict:
    """权限拒绝是尽力项：模型不配合时如实报告，不伪造成通过。"""
    denied_items = [item for item in tools if by_call[item["tool_call_id"]]["status"] == "denied"]
    if denied_items:
        return {
            "attempted": True,
            "denied_calls": [
                {
                    "name": item["name"],
                    "started_at": by_call[item["tool_call_id"]]["started_at"] is not None,
                    "timeline_status": item["status"],
                }
                for item in denied_items
            ],
            "outside_file_exists": OUTSIDE_FILE.exists(),
        }
    read_attempts = [item for item in tools if item["name"] == "Read"]
    if read_attempts:
        outside = read_attempts[0]
        detail = by_call[outside["tool_call_id"]]
        check(detail["status"] == "denied", "工作区外的 Read 没有被记为 denied", detail)
        check(
            detail["ended_at"] is not None,
            "权限拒绝的步骤没有结束时间",
            detail,
        )
        return {
            "attempted": True,
            "denied_calls": [outside["name"]],
            "outside_file_exists": OUTSIDE_FILE.exists(),
            "timeline_status": outside["status"],
        }
    return {
        "attempted": False,
        "reason": "模型没有发出工作区外的 Read 调用",
        "read_calls": [],
    }


async def scenario_compaction(instance: Instance, runtime: GatewayRuntime) -> dict:
    """场景 2：把上下文推过压缩阈值，核对压缩步骤与前后占用。

    记忆与技能额度都远小于上下文窗口，把材料推过阈值的唯一真实路径是输入本身：
    先发一轮大输入（阈值由 SDK 决定，这里构造的体量按实例的读数选），下一轮走 resume
    路径，轮首读数已经越线；自动压缩关闭时网关先做手动压缩。压缩是否真的发生以 SDK
    上报的边界信号为准：没有边界就不声称压缩，报告里如实写明。
    """

    filler = "\n".join(
        f"背景记录 {index:05d}：这一行只用于把上下文推过阈值。" for index in range(FILLER_LINES)
    )
    heavy_task, first = await start_task(
        runtime,
        instance,
        f"下面是一份只需要记住编号的参考资料，读完只回复 READY，不要调用任何工具。\n\n{filler}",
    )
    check(first["status"] == "done", "大输入轮没有正常结束", first)
    readings = [item for item in observation_dump(heavy_task) if item["run_id"] == first["run_id"]]
    check(len(readings) == 1, "大输入轮没有唯一的观测记录", readings)
    first_reading = readings[0]["context_after"]
    check(
        isinstance(first_reading, dict) and first_reading["used_percentage"] is not None,
        "大输入轮没有轮末读数",
        readings[0],
    )
    check(
        first_reading["used_percentage"] >= first_reading["threshold_percentage"],
        "大输入没有把上下文推过阈值，下一轮不会触发压缩",
        first_reading,
    )
    run = await start_turn(runtime, heavy_task, "本轮只回复 READY，不要调用任何工具。")
    check(run["status"] == "done", "压缩场景的轮次没有正常结束", run)

    observed = [item for item in observation_dump(heavy_task) if item["run_id"] == run["run_id"]]
    check(len(observed) == 1, "压缩场景没有对应的观测轮次", observed)
    observed = observed[0]
    compact_steps = [step for step in observed["steps"] if step["kind"] == "compact"]
    reading = observed["context_after"]
    check(isinstance(reading, dict), "压缩场景没有轮末读数", observed)
    report = {
        "task_id": heavy_task,
        "run_id": run["run_id"],
        "filler_chars": len(filler),
        "first_turn_status": first["status"],
        "first_turn_reading": first_reading,
        "context_before": observed["context_before"],
        "context_after": reading,
        "threshold_percentage": reading.get("threshold_percentage"),
        "auto_compact_enabled": reading.get("auto_compact_enabled"),
        "compact_steps": [
            {
                "status": step["status"],
                "detail": step["detail"],
                "started_at": step["started_at"],
                "ended_at": step["ended_at"],
            }
            for step in compact_steps
        ],
    }
    if not compact_steps:
        report["compaction"] = (
            "未发生：本轮读数没有越过阈值，或 SDK 未上报压缩边界信号（不伪造成压缩）"
        )
        return report
    check(len(compact_steps) == 1, "压缩步骤不是恰好一条", compact_steps)
    step = compact_steps[0]
    check(step["status"] in ("ok", "error"), "压缩步骤状态非法", step)
    detail = step["detail"]
    check(isinstance(detail, dict), "压缩步骤没有 detail", step)
    before = detail.get("before")
    after = detail.get("after")
    check(isinstance(before, dict), "压缩步骤没有前占用", detail)
    check(
        before.get("used_percentage") is not None and before["used_percentage"] >= 100,
        "压缩前的占用读数没有越过阈值",
        before,
    )
    # 后占用必须是真实读数（不是照抄前占用）。它的高度依赖读的时刻：手动压缩在压缩完成后
    # 立刻读，实测 CLI 1.1.38 在 autoCompact 关闭时这一刻仍报 100%（窗口占用只在下一轮
    # 请求时才按压缩后的上下文重算），因此这里只核对它有值、来自同一条读数口径，
    # 回落幅度留给报告呈现，不硬性断言。
    check(isinstance(after, dict), "压缩步骤没有后占用", detail)
    check(after.get("used_percentage") is not None, "压缩步骤的后占用没有读数", after)
    check(
        after.get("threshold_percentage") == before.get("threshold_percentage"),
        "压缩步骤的前后读数不是同一口径",
        detail,
    )
    report["after_still_at_limit"] = after["used_percentage"] >= 100
    report["compaction"] = (
        f"已记录压缩步骤：auto={bool(detail.get('auto'))}，before={before}，after={after}"
    )
    return report


def scenario_persistence(task_id: str) -> dict:
    """场景 3：重开数据库数据不变；删任务后观测随轮次级联消失。"""

    check(init_db() == SCHEMA_VERSION, "重新初始化没有停在当前 schema 版本", init_db())
    with session() as conn:
        summary_rows = conn.execute(
            "SELECT count(*) AS n FROM run_observations o JOIN agent_runs r "
            "ON r.run_id = o.run_id WHERE r.task_id = ?",
            (task_id,),
        ).fetchone()["n"]
        step_rows_count = conn.execute(
            "SELECT count(*) AS n FROM observation_steps s JOIN agent_runs r "
            "ON r.run_id = s.run_id WHERE r.task_id = ?",
            (task_id,),
        ).fetchone()["n"]
        # 触发域标识（KbStore 由本脚本直接写入）不应出现在观测里。
        leaked = [
            row["run_id"]
            for row in conn.execute(
                "SELECT run_id FROM observation_steps WHERE detail LIKE '%kb/%' "
                "OR code LIKE '%NEBULA%' OR detail LIKE '%/mnt/%'"
            )
        ]
    reopened = read_observations(task_id)
    check(summary_rows >= 1, "重开后轮次摘要丢失", summary_rows)
    check(step_rows_count >= 1, "重开后步骤丢失", step_rows_count)
    check(len(reopened) >= 1, "重开后观测读取为空", reopened)
    check(not leaked, "观测里出现了工具端点或资料正文的痕迹", leaked)

    tasks = SessionStore()
    tasks.delete_task(task_id)
    check(read_observations(task_id) == [], "删任务后观测记录还在", read_observations(task_id))
    with session() as conn:
        left = conn.execute(
            "SELECT count(*) AS n FROM observation_steps s LEFT JOIN agent_runs r "
            "ON r.run_id = s.run_id WHERE r.run_id IS NULL"
        ).fetchone()["n"]
    check(left == 0, "删任务后留下了孤立的观测步骤", left)
    return {
        "runs_after_reopen": summary_rows,
        "steps_after_reopen": step_rows_count,
        "after_delete": {"observations": [], "orphan_steps": left},
    }


async def verify(root: Path) -> dict:
    if get_settings().qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    instance = Instance(root)
    await instance.start()
    try:
        runtime = instance.runtime
        tools = await scenario_tools(instance, runtime)
        compaction = await scenario_compaction(instance, runtime)
        persistence = scenario_persistence(tools["task_id"])
        return {"tools": tools, "compaction": compaction, "persistence": persistence}
    finally:
        await instance.stop()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-observations-") as directory:
        # 进程内的会话助手按环境变量找数据库：先把实例目录指到临时目录，
        # 否则工具轨迹与观测会写进真实 `.data`。
        os.environ["PEBBLE_DATA_DIR"] = directory
        # 后台的标题与记忆判断沿用托管型号，不配就会走 auto：这里一并固定成验收型号，
        # 免得侧路调用落到当天额度用尽的型号上。
        os.environ.setdefault("PEBBLE_QODER_MODEL", MODEL)
        get_settings.cache_clear()
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
