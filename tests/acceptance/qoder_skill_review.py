"""真实 Qoder 模型验收 Skill 后台复盘：三种经历与后续加载。

运行：uv run --project server python -m tests.acceptance.qoder_skill_review
在临时实例中构造已持久化的任务轨迹，复盘与后续使用走真实 SDK/MCP 路径。
轨迹是脚本输入，不宣称前两轮由模型实际执行。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from server.agent.client import QoderGateway
from server.agent.mcp import ToolServer
from server.agent.toolset import ToolDeps, TurnKind
from server.config import Settings, get_settings
from server.db import init_db, session, write
from server.gateway.agent_contract import Turn
from server.sessions.service import SessionStore
from server.skills.models import ChangeAction, ChangeActor
from server.skills.review import SkillReviewScheduler, record_completed_turn
from server.skills.service import ChangeRequest, SkillService
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Instance:
    def __init__(self, root: Path):
        os.environ["PEBBLE_DATA_DIR"] = str(root)
        get_settings.cache_clear()
        self.path = root / "pebble.db"
        init_db(self.path)
        self.skills = SkillService(root, self.path)
        self.tasks = SessionStore(self.path)
        self.tool_server = ToolServer()
        self.settings = Settings(data_dir=root, tool_port=port())
        self.gateway = QoderGateway(
            ToolDeps(
                drafts=MailDraftStore(self.path),
                tasks=self.tasks,
                gmail=MockGmailClient(),
                skills=self.skills,
            ),
            self.tool_server,
            settings=self.settings,
        )
        self.scheduler = SkillReviewScheduler(self.skills, path=self.path, interval=2)

    def turn(self, task_id: str, number: int, user: str, assistant: str, *, tool=None):
        run_id = f"run-{number}"
        with session(self.path) as conn, write(conn):
            conn.execute(
                "INSERT INTO agent_runs (run_id,task_id,kind,input,status,created_at,finished_at) "
                "VALUES (?,?,'message',?,'done',?,?)",
                (run_id, task_id, json.dumps({"message": user}), "2026-09-26", "2026-09-26"),
            )
            base = number * 10
            for offset, role, text in ((1, "user", user), (3, "assistant", assistant)):
                conn.execute(
                    "INSERT INTO task_timeline_items "
                    "(item_id,task_id,run_id,sequence,kind,role,text,created_at) "
                    "VALUES (?,?,?,?,'text',?,?,?)",
                    (
                        f"item-{number}-{offset}",
                        task_id,
                        run_id,
                        base + offset,
                        role,
                        text,
                        "2026-09-26",
                    ),
                )
            if tool:
                name, arguments, result = tool
                conn.execute(
                    "INSERT INTO task_timeline_items "
                    "(item_id,task_id,run_id,sequence,kind,tool_call_id,tool_name,"
                    "tool_arguments,tool_status,tool_result,created_at) "
                    "VALUES (?,?,?,?,'tool',?,?,?,?,?,?)",
                    (
                        f"item-{number}-2",
                        task_id,
                        run_id,
                        base + 2,
                        f"call-{number}",
                        name,
                        json.dumps(arguments, ensure_ascii=False),
                        "ok",
                        result,
                        "2026-09-26",
                    ),
                )
            record_completed_turn(conn, run_id, task_id)
        return run_id

    async def review(self):
        job = self.scheduler.enqueue_if_due()
        assert job is not None
        assert self.scheduler.claim(job["id"]) is not None
        await self.scheduler.run(job["id"], self.gateway)
        return self.scheduler.get(job["id"])

    def assert_review_change(self, review: dict, skill_id: str) -> dict:
        changes = [
            change for change in self.skills.changes() if change["review_job_id"] == review["id"]
        ]
        assert len(changes) == 1 and changes[0]["status"] == "applied", changes
        change = changes[0]
        assert change["reason"] and change["evidence_item_ids"]
        with session(self.path) as conn:
            for item_id in change["evidence_item_ids"]:
                assert conn.execute(
                    "SELECT 1 FROM task_timeline_items i JOIN skill_review_turns t "
                    "ON t.run_id=i.run_id WHERE i.item_id=? AND t.seq>? AND t.seq<=?",
                    (item_id, review["from_seq"], review["through_seq"]),
                ).fetchone()
        assert any(
            version.change_id == change["id"]
            for version in self.skills.repository.versions(skill_id)
        )
        return change

    async def real_followup(self, prompt: str) -> tuple[str, list[dict]]:
        task_id = self.tasks.create_task("后续同类任务")["task_id"]
        run_id = f"run-{uuid4().hex}"
        with session(self.path) as conn, write(conn):
            conn.execute(
                "INSERT INTO agent_runs (run_id,task_id,kind,input,status,created_at,started_at) "
                "VALUES (?,?,'message',?,'running',?,?)",
                (run_id, task_id, json.dumps({"message": prompt}), "2026-09-26", "2026-09-26"),
            )
        texts = []
        async for event in self.gateway.stream_turn(
            Turn(
                kind=TurnKind.MESSAGE,
                task_id=task_id,
                run_id=run_id,
                sdk_session_id=None,
                message=prompt,
                db_path=self.path,
            )
        ):
            if event["type"] == "text":
                texts.append(event["text"])
            if event["type"] == "error":
                raise AssertionError(event["message"])
        return "".join(texts), self.skills.task_skill_usage(task_id)


async def scenario_create(root: Path) -> dict:
    instance = Instance(root)
    task_id = instance.tasks.create_task("周报纠正")["task_id"]
    instance.turn(task_id, 1, "帮我整理周报。", "我先写摘要，再核对日历。")
    instance.turn(
        task_id,
        2,
        "这个顺序不对。整理周报要先核对日历里的已完成事项，再写摘要，最后列下周安排。",
        "已核对日历：完成了项目讨论。摘要：本周完成项目讨论。下周安排：准备评审。",
        tool=("calendar_list_events", {"range": "this_week"}, "项目讨论已完成；下周评审"),
    )
    async with instance.tool_server.listen("127.0.0.1", instance.settings.tool_port):
        review = await instance.review()
        created = [s for s in instance.skills.repository.load_all() if s.origin.value == "review"]
        assert review["status"] == "completed" and len(created) == 1, instance.skills.changes()
        assert "日历" in created[0].body and "摘要" in created[0].body
        change = instance.assert_review_change(review, created[0].skill_id)
        answer, usage = await instance.real_followup(
            "请只复述周报整理的操作步骤，不需要查询任何真实资料，也不要执行这些步骤。"
        )
        assert any(item["skill_id"] == created[0].skill_id for item in usage), (answer, usage)
        assert answer.find("日历") <= answer.find("摘要"), answer
        return {
            "skill_id": created[0].skill_id,
            "body": created[0].body,
            "evidence_item_ids": change["evidence_item_ids"],
            "answer": answer,
        }


async def scenario_transient(root: Path) -> dict:
    instance = Instance(root)
    task_id = instance.tasks.create_task("临时故障")["task_id"]
    instance.turn(task_id, 1, "查询今天的天气。", "天气接口暂时返回 503，尚无结果。")
    instance.turn(
        task_id,
        2,
        "请再查一次。",
        "重试后查到了天气。",
        tool=("weather_query", {"date": "today"}, "晴，20 度"),
    )
    async with instance.tool_server.listen("127.0.0.1", instance.settings.tool_port):
        review = await instance.review()
    assert review["status"] == "completed"
    assert instance.skills.repository.load_all() == [], instance.skills.changes()
    assert instance.skills.changes() == []
    return {"result": review["result_summary"]}


async def scenario_update(root: Path) -> dict:
    instance = Instance(root)
    instance.skills.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload={
                "skill_id": "weekly-report",
                "name": "周报整理",
                "description": "整理本周工作周报",
                "body": "先列本周事项，再写摘要。",
            },
            actor=ChangeActor.FOREGROUND,
            reason="用户明确要求创建",
        )
    )
    task_id = instance.tasks.create_task("周报缺步")["task_id"]
    run_id = instance.turn(task_id, 1, "按周报技能整理。", "列了事项并写摘要，漏掉了下周安排。")
    with session(instance.path) as conn, write(conn):
        conn.execute(
            "INSERT INTO skill_loads (run_id,task_id,skill_id,revision,source,loaded_at) "
            "VALUES (?,?,?,?,'manual',?)",
            (
                run_id,
                task_id,
                "weekly-report",
                instance.skills.get("weekly-report").revision,
                "2026-09-26",
            ),
        )
    instance.turn(
        task_id,
        2,
        "这个技能漏了一步：写完摘要还要检查并列出下周安排。刚才按这个顺序重做已经正确。",
        "本周事项：项目讨论。摘要：完成讨论。下周安排：准备评审。",
    )
    async with instance.tool_server.listen("127.0.0.1", instance.settings.tool_port):
        review = await instance.review()
    skill = instance.skills.get("weekly-report")
    assert review["status"] == "completed"
    assert "下周" in skill.body, instance.skills.changes()
    assert len(instance.skills.repository.load_all()) == 1
    change = instance.assert_review_change(review, "weekly-report")
    return {
        "body": skill.body,
        "evidence_item_ids": change["evidence_item_ids"],
        "version_count": len(instance.skills.repository.versions("weekly-report")),
    }


async def main():
    if Settings().qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    with tempfile.TemporaryDirectory(prefix="pebble-skill-review-") as temporary:
        root = Path(temporary)
        scenarios = {
            "create": scenario_create,
            "transient": scenario_transient,
            "update": scenario_update,
        }
        selected = sys.argv[1:] or list(scenarios)
        results = {}
        for name in selected:
            results[name] = await scenarios[name](root / name)
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
