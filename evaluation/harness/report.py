"""评测报告：JSON 存档 + Markdown 汇总，写入本次运行目录。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model import Assertion, TurnEvidence


@dataclass
class ScenarioRecord:
    """一个场景一次重复的完整结果。"""

    scenario_id: str
    title: str
    layer: str
    repetition: int
    duration_s: float
    assertions: list[Assertion] = field(default_factory=list)
    error: str | None = None
    evidence: list[TurnEvidence] = field(default_factory=list)

    @property
    def failed(self) -> list[Assertion]:
        """计分层的失败断言（不含取证自检）。"""
        return [a for a in self.assertions if not a.passed and not a.infra]

    @property
    def infra_failures(self) -> list[Assertion]:
        """取证自检失败：记基础设施故障，不影响场景判定（评测规格 §5.3）。"""
        return [a for a in self.assertions if not a.passed and a.infra]

    @property
    def red_line_violations(self) -> list[Assertion]:
        return [a for a in self.failed if a.red_line]

    @property
    def pending_reviews(self) -> list[Assertion]:
        return [a for a in self.failed if a.pending]

    @property
    def passed(self) -> bool:
        return self.error is None and not self.failed

    def usage_totals(self) -> dict[str, Any]:
        """跨轮汇总用量；取不到可靠数值时如实留空（不编造）。"""
        totals: dict[str, float] = {}
        for evidence in self.evidence:
            observation = evidence.observation or {}
            for key, value in (observation.get("usage_totals") or {}).items():
                if isinstance(value, int | float):
                    totals[key] = totals.get(key, 0.0) + value
        return totals


def _clip(text: str, limit: int) -> str:
    """压平空白并截断，报告里只呈现必要长度；完整内容在 JSON 与数据目录。"""
    flattened = " ".join(str(text).split())
    return flattened[:limit] + ("…" if len(flattened) > limit else "")


def _turn_json(evidence: TurnEvidence) -> dict[str, Any]:
    return {
        "kind": evidence.run.get("kind"),
        "status": evidence.run.get("status"),
        "duration_s": round(evidence.duration_s, 1),
        "message": evidence.message,
        "reply": evidence.reply_text,
        "tools": [
            {"name": name, "status": status, "arguments": arguments}
            for name, status, arguments in evidence.tool_calls()
        ],
        "usage": (evidence.observation or {}).get("usage_totals"),
    }


def _render_turn(idx: int, evidence: TurnEvidence) -> list[str]:
    run = evidence.run
    usage = (evidence.observation or {}).get("usage_totals")
    head = f"### 轮 {idx} · {run.get('kind')} · {run.get('status')} · {evidence.duration_s:.1f}s"
    if usage:
        head += f" · 用量 {usage}"
    lines = ["", head, ""]
    lines.append(f"**用户**：{_clip(evidence.message, 400)}")
    lines.append("")
    lines.append(f"**助手**：{_clip(evidence.reply_text, 2000)}")
    lines.append("")
    calls = evidence.tool_calls()
    if calls:
        lines.append("**工具调用**：")
        for name, status, arguments in calls:
            detail = ""
            if arguments:
                detail = " " + _clip(json.dumps(arguments, ensure_ascii=False), 160)
            lines.append(f"- `{name}`（{status}）{detail}")
    else:
        lines.append("**工具调用**：（无）")
    return lines


def write_report(
    run_dir: Path, records: list[ScenarioRecord], meta: dict[str, Any]
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "report.json"
    md_path = run_dir / "report.md"

    json_payload = {
        "meta": {**meta, "generated_at": datetime.now(UTC).isoformat()},
        "records": [
            {
                "scenario_id": r.scenario_id,
                "title": r.title,
                "layer": r.layer,
                "repetition": r.repetition,
                "duration_s": round(r.duration_s, 1),
                "passed": r.passed,
                "error": r.error,
                "usage_totals": r.usage_totals(),
                "turns": [_turn_json(e) for e in r.evidence],
                "assertions": [vars(a) for a in r.assertions],
            }
            for r in records
        ],
    }
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total = len(records)
    passed = sum(1 for r in records if r.passed)
    violations = [a for r in records for a in r.red_line_violations]
    infra_failures = [a for r in records for a in r.infra_failures]
    pending = [a for r in records for a in r.pending_reviews]
    lines = [
        "# 真实场景评测报告",
        "",
        f"- 时间：{meta.get('started_at', '')}　模型：{meta.get('model', '')}",
        f"- 结果：**{passed}/{total} 通过**；红线违规 **{len(violations)}**（要求恒为 0）；"
        f"取证自检异常 **{len(infra_failures)}**（不计分）",
        f"- 待人工复核：**{len(pending)}**（不计通过）",
        "",
    ]
    for record in records:
        if record.passed:
            status = "通过"
        elif record.pending_reviews and len(record.failed) == len(record.pending_reviews):
            status = "待复核"
        else:
            status = "失败"
        heading = f"{record.scenario_id} {record.title}（第 {record.repetition} 次）——{status}"
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(
            f"耗时 {record.duration_s:.0f}s；用量 {record.usage_totals() or '未记录'}"
        )
        if record.error:
            lines.append("")
            lines.append(f"运行错误：\n\n```\n{record.error}\n```")
        if record.evidence:
            lines.append("")
            lines.append("## 轮次详情")
            for idx, evidence in enumerate(record.evidence, start=1):
                lines.extend(_render_turn(idx, evidence))
        if record.failed:
            lines.append("")
            lines.append("未通过断言：")
            lines.append("")
            for assertion in record.failed:
                flag = "🔴" if assertion.red_line else "🟠" if assertion.pending else "❌"
                lines.append(f"- {flag} **{assertion.name}** {assertion.detail}")
        if record.infra_failures:
            lines.append("")
            lines.append("取证自检异常（不计分）：")
            lines.append("")
            for assertion in record.infra_failures:
                lines.append(f"- 🟡 **{assertion.name}** {assertion.detail}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path
