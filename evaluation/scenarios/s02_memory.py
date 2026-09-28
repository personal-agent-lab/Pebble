"""S2：教会长期偏好，隔几个任务后检查是否仍生效且不误记情绪。"""

from __future__ import annotations

import time
from typing import Any

from ..harness.checks import check_turn
from ..harness.model import Assertion, Scenario, ScenarioContext
from ..harness.review import review_task
from ..harness.seeds import SeedBuilder
from .common import seed_corpus

TEACH = (
    "这周乱死了。对了跟你说一下，我周二上午固定有部门例会，排事儿避开这个时段；"
    "还有我妈生日是 11 月 8 号，每年都得提前订蛋糕。"
)
FOLLOW_UP = "帮我看看下周二上午十点半能不能约个洗牙？"
CASUAL = "今天好累，随便聊聊。"


def _seed(builder: SeedBuilder) -> None:
    documents, user_md, memory_md = seed_corpus()
    # 本场景必须从未教过生日的状态起步；共享人格的其他事实照常保留。
    user_md = user_md.replace(
        "老家苏州，妈妈还在那边；每年体检，妈妈生日 11 月 8 日要提前订蛋糕。",
        "老家苏州，妈妈还在那边；每年体检。",
    )
    if "生日" in user_md or "周二上午固定" in user_md + memory_md:
        raise RuntimeError("S2 初始记忆已含待学习事实")
    builder.memory(user_md, memory_md)
    builder.kb_docs(list(documents))


def _contents(snapshot: dict[str, Any]) -> str:
    return "\n".join(str(snapshot[key]["content"]) for key in ("user", "memory"))


def _learned(snapshot: dict[str, Any]) -> bool:
    text = _contents(snapshot)
    birthday = "11 月 8" in text or "11月8" in text or "11/8" in text
    return birthday and "蛋糕" in text and "周二" in text and "上午" in text and "例会" in text


def _wait_learned(ctx: ScenarioContext) -> dict[str, Any]:
    deadline = time.monotonic() + ctx.settings.settle_timeout
    snapshot = ctx.client.get_memory()
    while not _learned(snapshot) and time.monotonic() < deadline:
        time.sleep(ctx.settings.poll_interval)
        snapshot = ctx.client.get_memory()
    return snapshot


def _run(ctx: ScenarioContext) -> list[Assertion]:
    taught = ctx.converse(message=TEACH)
    learned = _wait_learned(ctx)
    # 另开任务，避免依赖当前任务的 SDK 对话上下文；中间穿插真实对话。
    fillers = [
        ctx.converse(message="我在整理这周的安排，先随便聊两句。"),
        ctx.converse(message="下雨天总想喝点热的，你有什么建议？"),
    ]
    follow_up = ctx.converse(message=FOLLOW_UP)
    casual = ctx.converse(message=CASUAL)
    # 轮内记忆判断与主回答并行；给末轮判断留出完成时间，再核对未入库。
    time.sleep(min(20.0, ctx.settings.settle_timeout))
    final_memory = ctx.client.get_memory()
    assertions = [
        item for turn in (taught, *fillers, follow_up, casual)
        for item in check_turn(turn)
    ]
    assertions.extend([
        Assertion("long_term_facts_saved", _learned(learned), "两条长期信息应在后续任务前入库"),
        Assertion("memory_budget", final_memory["memory"]["usage"]["chars"] <= 2200,
                  "MEMORY.md 不得超过 2200 字符"),
        Assertion("temporary_mood_not_saved", not any(
            phrase in _contents(final_memory)
            for phrase in ("今天好累", "今天很累", "今天累", "疲惫", "这周乱死了")
        ),
                  "一次性疲惫状态不应成为长期记忆"),
        Assertion("no_false_booking", not any(
            phrase in follow_up.reply_text for phrase in ("约好了", "预约成功", "已经预约")
        ), "未连接日历时不得声称已经预约"),
    ])
    assertions.extend(review_task(
        task=f"先教偏好与生日，隔两个任务后问：{FOLLOW_UP}；最后说：{CASUAL}",
        expected=(
            "记住周二上午例会和妈妈 11 月 8 日生日需提前订蛋糕；"
            "洗牙时间与例会冲突，应指出冲突或建议改期，不声称已预约；"
            "对疲惫自然回应，不把当天情绪写成长期事实。"
        ),
        reference=f"学习后记忆：{_contents(learned)}\n最终记忆：{_contents(final_memory)}",
        evidence=[taught, *fillers, follow_up, casual],
        work_dir=ctx.data_dir / "evaluation-review",
    ))
    return assertions


SCENARIO = Scenario(
    id="S2", title="教偏好与生日", layer="S", repeat=3, seed=_seed, run=_run,
)
