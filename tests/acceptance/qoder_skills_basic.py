"""真实 Qoder 模型的技能基础验收（spec §12 场景 1、2）。

显式运行，不进入 pytest；使用临时实例目录，不碰 `.data`。依次验证：

1. 手工技能生效：管理页创建的技能进入每轮目录，相关任务里模型自己读取正文并按正文
   规定（目录里看不到的）格式执行，加载记录绑定当时的内容版本；无关任务不加载它。
2. 明确学习：一次被纠正的做法经 `skill_manage` 落成 `origin=explicit` 的技能，正文写
   做法不写一次性细节，Git 留提交；新会话遇到同类任务时按目录匹配、读取并照着做。
3. 选择边界：手动装配带 revision 绑定、排除与关闭自动匹配在装配层强制执行。

运行：`uv run --project server python -m tests.acceptance.qoder_skills_basic`
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import tempfile
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI

from server.agent.client import QoderGateway
from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.agent.toolset import ToolDeps, TurnKind
from server.config import Settings
from server.db import init_db
from server.gateway.agent_contract import Turn
from server.sessions.service import SessionStore
from server.skills.models import ChangeAction, ChangeActor, SkillOrigin
from server.skills.runtime import SKIPPED_TITLE, catalog_material, manual_materials
from server.skills.service import ChangeRequest, SkillService
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient

MINUTES_BODY = """## 适用场景

用户要求把会议记录整理成正式纪要时使用。

## 固定格式

1. 先用一行写会议主题与日期。
2. 每条议题单独一行，以「议题：」开头。
3. 所有待办集中放在最后，每行以「待办：」开头。
4. 全文最后一行固定写「——纪要结束——」，不写别的内容。
"""

UNRELATED_ASK = "把 hello 翻译成法语，只回答译文。"


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Instance:
    """临时实例：真实网关 + 真实技能服务，工具端点在本机随机端口上单独监听。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = root / "pebble.db"
        init_db(self.database)
        self.skills = SkillService(root, self.database)
        self.tasks = SessionStore(self.database)
        self.gateway = QoderGateway(
            ToolDeps(
                drafts=MailDraftStore(self.database),
                tasks=self.tasks,
                gmail=MockGmailClient(),
                skills=self.skills,
            ),
            ToolServer(),
            settings=Settings(data_dir=root, tool_port=available_port()),
        )
        self.materials: list[dict] = []

    async def turn(
        self,
        task_id: str,
        message: str,
        *,
        session: str | None = None,
        skills: tuple[str, ...] = (),
        excluded: tuple[str, ...] = (),
        auto_match: bool = True,
    ) -> tuple[str, str]:
        """跑一轮真实对话，返回回答文本与 SDK 会话标识。"""

        texts: list[str] = []
        sdk_session_id = session
        # 生产路径由网关登记运行记录并给出 run_id；验收直接驱动网关，这里补一个。
        run_id = f"run-{uuid4().hex[:12]}"
        async for event in self.gateway.stream_turn(
            Turn(
                kind=TurnKind.MESSAGE,
                task_id=task_id,
                run_id=run_id,
                sdk_session_id=session,
                message=message,
                skills=skills,
                excluded_skill_ids=excluded,
                auto_match=auto_match,
            )
        ):
            if event["type"] == "text":
                texts.append(event["text"])
            elif event["type"] == "session":
                sdk_session_id = event["sdk_session_id"]
            elif event["type"] == "error":
                raise RuntimeError(event["message"])
        assert sdk_session_id is not None
        return "".join(texts).strip(), sdk_session_id

    def create_skill(self, skill_id: str, name: str, description: str, body: str) -> dict:
        """等价于管理页创建：`actor=user`，直接生效。"""

        return self.skills.record_change(
            ChangeRequest(
                action=ChangeAction.CREATE,
                payload={
                    "skill_id": skill_id,
                    "name": name,
                    "description": description,
                    "body": body,
                },
                actor=ChangeActor.USER,
                reason="验收脚本：管理页创建",
            )
        )

    def usage(self, task_id: str) -> list[tuple[str, str, str]]:
        return [
            (row["skill_id"], row["source"], row["revision"])
            for row in self.skills.task_skill_usage(task_id)
        ]


def check(condition: bool, message: str, evidence: object) -> None:
    if not condition:
        raise AssertionError(f"{message}：证据={evidence!r}")


def _first_index(text: str, keywords: tuple[str, ...]) -> int | None:
    """一组同义说法里最先出现的位置；都没出现时为 None。"""

    positions = [text.index(word) for word in keywords if word in text]
    return min(positions) if positions else None


async def scenario_manual_skill(instance: Instance) -> dict:
    """场景 1：手工技能在相关任务里生效，在无关任务里不加载。"""

    created = instance.create_skill(
        "meeting-notes", "会议纪要", "把会议记录整理成固定格式的正式纪要", MINUTES_BODY
    )
    check(created["status"] == "applied", "手工技能没有直接生效", created)
    skill = instance.skills.get("meeting-notes")
    check(skill.managed is False, "用户手写的技能应为受保护", skill.managed)

    # 目录常驻：每轮注入的材料里能看到目录条目，但看不到正文。
    catalog = catalog_material(instance.skills)
    check(catalog is not None, "技能目录材料没有装配", catalog)
    entry = catalog.content["skills"][0]
    check(entry["skill_id"] == "meeting-notes", "目录里没有这个技能", catalog.content)
    check("议题：" not in json.dumps(catalog.content), "目录材料不应包含正文", catalog.content)

    task_id = instance.tasks.create_task("整理会议纪要")["task_id"]
    answer, _ = await instance.turn(
        task_id,
        "把这段会议记录整理成纪要：产品周会，确认 9 月 30 日发布；张三负责回归测试；"
        "李四负责更新文档。",
    )
    usage = instance.usage(task_id)
    check(
        ("meeting-notes", "auto", skill.revision) in usage,
        "模型没有读取技能正文（或加载记录未绑定当前版本）",
        {"usage": usage, "revision": skill.revision, "answer": answer},
    )
    check("——纪要结束——" in answer, "回答没有遵循技能正文规定的固定格式", answer)
    check("待办：" in answer, "回答缺少正文要求的待办分节", answer)

    # 无关任务：不加载这个技能。
    other_task = instance.tasks.create_task("翻译一句话")["task_id"]
    other_answer, _ = await instance.turn(other_task, UNRELATED_ASK)
    check(instance.usage(other_task) == [], "无关任务加载了技能", instance.usage(other_task))
    check("——纪要结束——" not in other_answer, "无关任务被技能格式影响", other_answer)
    return {
        "catalog": catalog.content,
        "usage": usage,
        "answer": answer,
        "unrelated_answer": other_answer,
    }


async def scenario_explicit_learning(instance: Instance) -> dict:
    """场景 2：一次被纠正的排查过程经对话沉淀成技能，并在新会话里生效。"""

    before = {skill.skill_id for skill in instance.skills.repository.load_all()}
    task_id = instance.tasks.create_task("投屏排查")["task_id"]
    opening = "我在会议室投屏到电视一直没画面，今天下午两点要用，帮我说说可能的原因。"
    first, session = await instance.turn(task_id, opening)
    correction = (
        "顺序不对：我们这台电视先要确认输入源选的是不是正确的 HDMI 口，"
        "再确认笔记本分辨率有没有超过 1080p；按这个顺序重新说一遍。"
    )
    second, session = await instance.turn(task_id, correction, session=session)
    learn_request = "把刚才这个投屏排查的顺序记成一个技能，以后遇到同类问题照着做。"
    asked, session = await instance.turn(task_id, learn_request, session=session)

    created = [
        skill
        for skill in instance.skills.repository.load_all()
        if skill.skill_id not in before
    ]
    check(len(created) == 1, "明确学习没有恰好沉淀一个技能", [s.skill_id for s in created])
    skill = created[0]
    check(skill.origin is SkillOrigin.EXPLICIT, "沉淀的技能来源不是 explicit", skill.origin)
    check(skill.managed is True, "Agent 沉淀的技能应可由复盘直接更新", skill.managed)
    check(skill.state.value == "active", "沉淀的技能不是启用状态", skill.state)
    for keyword in ("输入源", "分辨率"):
        check(keyword in skill.body, f"技能正文没有写下纠正后的做法：{keyword}", skill.body)
    # 沉淀的是做法不是轨迹：不复制消息原文，也不留当时的时间等一次性细节。
    for source in (opening, correction, learn_request):
        for sentence in re.split(r"[。；\n]", source):
            sentence = sentence.strip()
            if len(sentence) >= 12:
                check(sentence not in skill.body, "技能正文复制了消息原文", sentence)
    check(
        "两点" not in skill.body and "今天下午" not in skill.body,
        "技能正文保留了一次性时间细节",
        skill.body,
    )

    versions = instance.skills.repository.versions(skill.skill_id)
    check(
        versions[0].actor == "foreground" and versions[0].change_id,
        "落盘的提交缺少 actor 与变更标识",
        [(v.actor, v.change_id, v.reason) for v in versions],
    )
    raw = (instance.skills.repository.skills_root / skill.skill_id / "SKILL.md").read_text(
        encoding="utf-8"
    )
    frontmatter = {
        line.split(":", 1)[0]
        for line in raw.split("---")[1].strip().splitlines()
    }
    check(
        frontmatter >= {"name", "description", "origin", "managed", "state", "created_at"},
        "frontmatter 字段不全",
        sorted(frontmatter),
    )

    # 新会话遇到同类任务：目录匹配 → 读取正文 → 按纠正后的顺序回答。
    fresh_task = instance.tasks.create_task("再次投屏")["task_id"]
    follow_up, _ = await instance.turn(
        fresh_task, "我的笔记本连会议室电视又没有画面，按你记录的做法告诉我应该先查什么。"
    )
    usage = instance.usage(fresh_task)
    check(
        (skill.skill_id, "auto", skill.revision) in usage,
        "新会话没有加载沉淀的技能",
        {"usage": usage, "answer": follow_up},
    )
    # 顺序按纠正后的做法：先看输入源，再看分辨率/输出参数；同义说法都算。
    first_step = _first_index(follow_up, ("输入源", "信号源", "HDMI", "Input", "Source"))
    second_step = _first_index(
        follow_up, ("分辨率", "输出参数", "1080p", "1920×1080", "刷新率")
    )
    check(
        first_step is not None and second_step is not None and first_step < second_step,
        "新会话没有按技能里的顺序排查",
        {"输入源位置": first_step, "分辨率位置": second_step, "answer": follow_up},
    )
    return {
        "skill_id": skill.skill_id,
        "frontmatter": sorted(frontmatter),
        "body": skill.body,
        "versions": [(v.actor, v.change_id, v.reason) for v in versions],
        "first_answer": first,
        "corrected_answer": second,
        "learn_request_answer": asked,
        "follow_up_answer": follow_up,
    }


def scenario_selection_boundaries(instance: Instance) -> dict:
    """边界：手动装配绑定当前版本，排除与关闭自动匹配在装配层强制执行。"""

    instance.create_skill(
        "weekly-report", "周报整理", "按固定分节整理本周周报", "## 步骤\n\n1. 先查日程\n"
    )
    task_id = instance.tasks.create_task("手动选择")["task_id"]
    materials = manual_materials(
        instance.skills, ["weekly-report"], task_id=task_id, run_id="run-manual"
    )
    assert len(materials) == 1
    usage = instance.usage(task_id)
    revision = instance.skills.get("weekly-report").revision
    check(usage == [("weekly-report", "manual", revision)], "手动装配没有绑定当前版本", usage)

    excluded = catalog_material(instance.skills, excluded_skill_ids={"weekly-report"})
    listed = [] if excluded is None else [item["skill_id"] for item in excluded.content["skills"]]
    check("weekly-report" not in listed, "排除的技能仍在目录里", listed)

    # 直接改磁盘的技能退出目录与装配（契约 §8）。
    path = instance.skills.repository.skills_root / "weekly-report" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n手改一行\n", encoding="utf-8")
    dirty_catalog = catalog_material(instance.skills)
    dirty_listed = (
        []
        if dirty_catalog is None
        else [item["skill_id"] for item in dirty_catalog.content["skills"]]
    )
    check("weekly-report" not in dirty_listed, "被直接改动的技能仍留在目录", dirty_listed)
    dirty_materials = manual_materials(
        instance.skills, ["weekly-report"], task_id=task_id, run_id="run-dirty"
    )
    # 正文不装配，但告知照常：只剩一份"未装配"材料，说明跳过与原因。
    check(
        [material.title for material in dirty_materials] == [SKIPPED_TITLE],
        "被直接改动的技能仍被手动装配或缺少未装配告知",
        [material.title for material in dirty_materials],
    )
    return {"manual_usage": usage, "catalog_after_edit": dirty_listed}


async def verify(root: Path) -> dict:
    if Settings().qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    instance = Instance(root)
    server = uvicorn.Server(
        uvicorn.Config(
            _tool_app(instance), host="127.0.0.1", port=instance.gateway.settings.tool_port,
            log_level="warning",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.01)
    try:
        report = {
            "manual_skill": await scenario_manual_skill(instance),
            "explicit_learning": await scenario_explicit_learning(instance),
            "selection": scenario_selection_boundaries(instance),
        }
        report["skill_files"] = sorted(
            str(path.relative_to(root))
            for path in instance.skills.repository.skills_root.rglob("*")
            if path.is_file()
        )
        return report
    finally:
        server.should_exit = True
        await serving


def _tool_app(instance: Instance) -> FastAPI:
    app = FastAPI()
    app.mount(MCP_MOUNT_PATH, instance.gateway.tool_server)
    return app


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-skills-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
