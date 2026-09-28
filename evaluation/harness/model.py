"""场景与断言的数据模型。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import Attachment, PebbleClient
from .config import EvalSettings
from .seeds import SeedBuilder


@dataclass(frozen=True)
class Assertion:
    """一条断言，属评测规格 §5 三层之一：

    计分断言（默认）；红线（`red_line=True`，一票否决）；
    取证自检（`infra=True`，失败记基础设施故障，不计入场景判定）。
    """

    name: str
    passed: bool
    detail: str = ""
    red_line: bool = False
    infra: bool = False
    pending: bool = False


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具调用：名称、状态、参数与返回文本。

    `result` 是产品时间线里记的返回内容（`timeline.py` 对外返回 `result`），
    断言可以据此确认"事实确实来自这次调用"，而不是只看模型说了什么。
    """

    name: str
    status: str
    arguments: Any
    result: str

    @property
    def ok(self) -> bool:
        """状态语义与产品一致：`ok` 才算成功，`error` 含闸门拒绝与执行失败。"""
        return self.status == "ok"

    def returned(self, needle: str) -> bool:
        return needle in self.result


@dataclass
class TurnEvidence:
    """一轮对话的取证快照：用户消息、终态 run、时间线、轮次观测与助手回复文本。"""

    task_id: str
    run: dict[str, Any]
    timeline_items: list[dict[str, Any]]
    observation: dict[str, Any] | None
    all_observations: list[dict[str, Any]]
    reply_text: str
    attachments: bool = False
    message: str = ""
    duration_s: float = 0.0

    def tool_items(self) -> list[tuple[str, str]]:
        """本轮全部工具调用的（名称，状态）；被拒绝的调用也在时间线里。"""
        return [(name, status) for name, status, _ in self.tool_calls()]

    def tool_calls(self) -> list[tuple[str, str, Any]]:
        """本轮全部工具调用的（名称，状态，参数）；供报告呈现细节。"""
        return [(call.name, call.status, call.arguments) for call in self.invocations()]

    def invocations(self) -> list[ToolInvocation]:
        """本轮全部工具调用，含返回文本；供断言锚定真实状态与工具返回的事实。"""
        run_id = self.run.get("run_id")
        return [
            ToolInvocation(
                name=str(item["name"]),
                status=str(item.get("status")),
                arguments=item.get("arguments"),
                result=str(item.get("result") or ""),
            )
            for item in self.timeline_items
            if item.get("kind") == "tool" and item.get("run_id") == run_id
        ]


@dataclass
class ScenarioContext:
    """场景运行环境：驱动器 + 配置 + 数据目录；converse 封装"发一轮、等完成、取证"。"""

    client: PebbleClient
    settings: EvalSettings
    data_dir: Path
    evidence: list[TurnEvidence] = field(default_factory=list)

    def converse(
        self,
        *,
        message: str,
        task_id: str | None = None,
        files: list[Attachment] | None = None,
        selection: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        on_started: Callable[[str, str], None] | None = None,
    ) -> TurnEvidence:
        started = time.monotonic()
        if task_id is None:
            payload = self.client.create_task(
                message=message,
                model=self.settings.effective_model,
                files=files,
                selection=selection,
            )
            task_id = payload["task"]["task_id"]
            run = payload["run"]
        else:
            run = self.client.send_message(
                task_id, message=message, files=files, selection=selection, target=target
            )
        run_id = run["run_id"]
        if on_started is not None:
            on_started(task_id, run_id)
        final_run = self.client.wait_turn(
            task_id,
            run_id,
            timeout=self.settings.turn_timeout,
            poll_interval=self.settings.poll_interval,
        )
        all_observations = self.client.observations(task_id)
        observation = self.client.wait_observation(
            task_id,
            run_id,
            timeout=self.settings.settle_timeout,
            poll_interval=self.settings.poll_interval,
        )
        items = self.client.timeline(task_id)
        reply_text = "\n".join(
            item.get("text", "")
            for item in items
            if item.get("kind") == "text"
            and item.get("role") == "assistant"
            and item.get("run_id") == run_id
        )
        evidence = TurnEvidence(
            task_id=task_id,
            run=final_run,
            timeline_items=items,
            observation=observation,
            all_observations=all_observations,
            reply_text=reply_text,
            attachments=bool(files),
            message=message,
            duration_s=time.monotonic() - started,
        )
        self.evidence.append(evidence)
        return evidence


@dataclass(frozen=True)
class Scenario:
    """一个可重复运行的评测场景：种子铺设 + 剧本执行，产出断言列表。"""

    id: str
    title: str
    layer: str
    repeat: int
    seed: Callable[[SeedBuilder], None]
    run: Callable[[ScenarioContext], list[Assertion]]
