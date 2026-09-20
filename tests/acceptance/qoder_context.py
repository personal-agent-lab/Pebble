"""真实 Qoder SDK 短期上下文验收。

本脚本会调用真实模型并消耗账号额度，不进入 pytest。默认验证多轮、独立进程恢复与逐轮材料
注入；加 ``--compact`` 后生成长测试输入，执行一次手动压缩并检查压缩后的任务信息。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from qodercn_agent_sdk import (
    AssistantMessage,
    HookMatcher,
    QoderAgentOptions,
    QoderSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    access_token,
)

from server.agent.client import CONFIG_DIR_ENV, turn_context_hooks
from server.agent.prompt import BASE_PROMPT
from server.config import Settings


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    text: str


def options(
    root: Path,
    settings: Settings,
    *,
    resume: str | None = None,
    system_prompt: str = BASE_PROMPT,
    additional_context: str = "",
    hooks=None,
) -> QoderAgentOptions:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ[CONFIG_DIR_ENV] = str(root / "config")
    return QoderAgentOptions(
        tools=[],
        allowed_tools=[],
        mcp_servers={},
        allowed_mcp_server_names=[],
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        system_prompt=system_prompt,
        hooks=hooks if hooks is not None else turn_context_hooks(additional_context),
        cwd=workspace,
        resume=resume,
        include_partial_messages=False,
        auth=access_token(settings.qoder_token.get_secret_value()),
        model=settings.qoder_model,
    )


async def run_turn(
    root: Path,
    prompt: str,
    *,
    resume: str | None = None,
    system_prompt: str = BASE_PROMPT,
    additional_context: str = "",
) -> TurnResult:
    settings = Settings()
    if settings.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    session_id = resume
    text_parts: list[str] = []
    final_text = ""
    async with QoderSDKClient(
        options(
            root,
            settings,
            resume=resume,
            system_prompt=system_prompt,
            additional_context=additional_context,
        )
    ) as client:
        if resume is not None:
            usage = await client.get_context_usage()
            if "contextWindow" not in usage or "autoCompact" not in usage:
                raise AssertionError(f"恢复后上下文状态字段不完整：{usage!r}")
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id") or session_id
            elif isinstance(message, AssistantMessage):
                text_parts.extend(
                    block.text for block in message.content if isinstance(block, TextBlock)
                )
            elif isinstance(message, ResultMessage):
                session_id = message.session_id or session_id
                if message.is_error:
                    raise RuntimeError(message.result or "Qoder 调用失败")
                final_text = (message.result or "").strip()
    if session_id is None:
        raise RuntimeError("Qoder 未返回 session_id")
    return TurnResult(session_id=session_id, text=final_text or "".join(text_parts).strip())


def require_text(result: TurnResult, *values: str) -> None:
    missing = [value for value in values if value not in result.text]
    if missing:
        raise AssertionError(f"回答缺少 {missing!r}：{result.text!r}")


def run_in_fresh_process(state_path: Path) -> TurnResult:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.acceptance.qoder_context",
            "--resume-worker",
            str(state_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    payload = json.loads(completed.stdout)
    return TurnResult(session_id=payload["session_id"], text=payload["text"])


async def verify_compaction(root: Path) -> dict:
    settings = Settings()
    if settings.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    observed: list[tuple[str, str]] = []

    async def before(input_data, _tool_use_id, _hook_context):
        observed.append(("before", input_data["trigger"]))
        return {}

    async def after(input_data, _tool_use_id, _hook_context):
        observed.append(("after", input_data["trigger"]))
        return {}

    hooks = {
        "PreCompact": [HookMatcher(hooks=[before])],
        "PostCompact": [HookMatcher(hooks=[after])],
    }
    filler = "\n".join(f"背景记录 {index:05d}：此行仅用于填充上下文。" for index in range(14_000))
    prompt = (
        "任务代号 COMPACT-481。关键约束：必须保留蓝色标签。"
        "未完成事项：输出最终检查单。\n"
        f"{filler}\n"
        "请记住任务代号、关键约束和未完成事项，本轮只回复 READY。"
    )
    compact_boundary = False
    answers: list[str] = []
    async with QoderSDKClient(options(root, settings, hooks=hooks)) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage) and message.is_error:
                raise RuntimeError(message.result or "长上下文调用失败")
        before_usage = await client.get_context_usage()

        await client.query("/compact")
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
                compact_boundary = True
            elif isinstance(message, ResultMessage) and message.is_error:
                raise RuntimeError(message.result or "手动压缩失败")

        await client.query("请说出任务代号、关键约束和未完成事项。只列这三项。")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                answers.extend(
                    block.text for block in message.content if isinstance(block, TextBlock)
                )
            elif isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError(message.result or "压缩后回忆失败")
                if message.result:
                    answers.append(message.result)

    answer = "\n".join(answers)
    for value in ("COMPACT-481", "蓝色标签", "最终检查单"):
        if value not in answer:
            raise AssertionError(f"压缩后回答缺少 {value!r}：{answer!r}")
    if not compact_boundary or observed != [("before", "manual"), ("after", "manual")]:
        raise AssertionError(f"未观察到完整压缩边界：boundary={compact_boundary}, hooks={observed}")
    return {
        "context_used_before_percent": before_usage["contextWindow"]["usedPercentage"],
        "auto_compact": before_usage["autoCompact"],
        "manual_compact_hooks": observed,
        "compact_boundary": compact_boundary,
        "recall": "passed",
    }


async def verify(root: Path, *, compact: bool) -> dict:
    first = await run_turn(
        root,
        "当前任务是压缩通知。待修改原文：项目晨星的交付标识为 ORBIT-731，必须在周二前"
        "完成审核。记住这些条件，本轮只回复 READY。",
    )
    require_text(first, "READY")

    state_path = root / "resume-check.json"
    state_path.write_text(
        json.dumps(
            {
                "root": str(root),
                "session_id": first.session_id,
                "prompt": (
                    "将刚才的原文改为不超过35个汉字，保留之前明确的全部硬性信息，只输出结果。"
                ),
            },
            ensure_ascii=False,
        )
    )
    resumed = run_in_fresh_process(state_path)
    require_text(resumed, "ORBIT-731", "周二前")
    if resumed.session_id != first.session_id:
        raise AssertionError("独立进程恢复后 session_id 发生变化")

    fresh_material = await run_turn(
        root,
        "请用一句话称呼并问候我。",
        resume=first.session_id,
        additional_context="## 关于你\n用户希望被称为林舟。",
    )
    if "林舟" not in fresh_material.text:
        raise AssertionError(f"恢复会话没有应用最新材料：{fresh_material.text!r}")

    old_prompt = "用户发送“校验规则”时，只回复 OLD-RULE。"
    new_prompt = "用户发送“校验规则”时，只回复 NEW-RULE。"
    old = await run_turn(root, "校验规则", system_prompt=old_prompt)
    replaced = await run_turn(
        root,
        "校验规则",
        resume=old.session_id,
        system_prompt=new_prompt,
    )
    require_text(old, "OLD-RULE")

    report = {
        "multi_turn_context": "passed",
        "resume_after_python_process_restart": "passed",
        "fresh_turn_material_on_resume": "passed",
        "resumed_base_system_prompt_observation": replaced.text,
        "resumed_base_system_prompt_policy": "not_relied_upon_use_session_start_context",
    }
    if compact:
        report["compaction"] = await verify_compaction(root / "compact")
    return report


async def resume_worker(state_path: Path) -> None:
    state = json.loads(state_path.read_text())
    result = await run_turn(
        Path(state["root"]),
        state["prompt"],
        resume=state["session_id"],
    )
    print(json.dumps({"session_id": result.session_id, "text": result.text}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--resume-worker", type=Path)
    args = parser.parse_args()
    if args.resume_worker:
        asyncio.run(resume_worker(args.resume_worker))
        return
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-context-") as directory:
        report = asyncio.run(verify(Path(directory), compact=args.compact))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
