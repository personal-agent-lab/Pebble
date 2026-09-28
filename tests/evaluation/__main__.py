"""最小真实模型评测：复用验收装配，每次独立实例，逐次保存证据，不自动重试。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from server.config import Settings, get_settings
from server.sessions.observations import read_observations
from tests.acceptance.qoder_kb_search import Harness, available_port

CASES = Path(__file__).with_name("cases.json")


def score(step: dict, answer: str, reads: list[str], run: dict) -> dict[str, bool]:
    """只对明确事实做断言，不把措辞匹配伪装成开放式语义评审。"""
    paths = {value.split(":", 1)[1].removeprefix("kb/") for value in reads}
    return {
        "run_completed": run["status"] == "done",
        "required_facts": all(value in answer for value in step["contains"]),
        "no_stale_or_unrelated_facts": all(value not in answer for value in step["excludes"]),
        "read_expected_documents": set(step["read_paths"]) <= paths,
    }


def summarize(results: list[dict]) -> dict:
    ids = sorted({result["case_id"] for result in results})
    return {
        "attempts": len(results),
        "passed": sum(result["status"] == "passed" for result in results),
        "failed": sum(result["status"] == "failed" for result in results),
        "errors": sum(result["status"] == "error" for result in results),
        "cases_all_attempts_passed": sum(
            all(result["status"] == "passed" for result in results if result["case_id"] == case)
            for case in ids
        ),
        "cases": len(ids),
    }


async def attempt(case: dict, model: str, timeout: float) -> dict:
    started = time.monotonic()
    result = {"case_id": case["id"], "status": "error", "steps": [], "error": None}
    # 独立随机事实避免沿用固定答案；替换前后均保存在本地报告中。
    values = {key: f"QL-{uuid4().hex[:12]}" for key in ("old", "current", "other")}
    case = json.loads(
        json.dumps(case, ensure_ascii=False)
        .replace("{old}", values["old"])
        .replace("{current}", values["current"])
        .replace("{other}", values["other"])
    )
    result["fixture"] = case
    previous = os.environ.get("PEBBLE_DATA_DIR")
    with tempfile.TemporaryDirectory(prefix="pebble-eval-") as directory:
        os.environ["PEBBLE_DATA_DIR"] = directory
        get_settings.cache_clear()
        harness = None
        task_id = None
        try:
            root = Path(directory)
            settings = Settings(data_dir=root, tool_port=available_port(), qoder_model=model)
            harness = Harness(root, settings, settings.tool_port)
            async with asyncio.timeout(timeout):
                await harness.start()
                documents = {}
                for document in case["documents"]:
                    documents[document["path"]] = harness.kb.save(**document)
                task_id = harness.tasks.create_task(case["description"], model=model)["task_id"]
                for step in case["steps"]:
                    if update := step.get("update"):
                        document = documents[update["path"]]
                        documents[update["path"]] = harness.kb.update(
                            doc_id=document["id"],
                            expected_version=document["version"],
                            body=update["body"],
                        )
                    harness.reads.clear()
                    harness.searches.clear()
                    run = harness.service.submit_message(task_id, step["message"])
                    while True:
                        run = harness.service.get_run(run["run_id"])
                        if run["status"] in {"done", "error", "interrupted"}:
                            break
                        await asyncio.sleep(0.1)
                    timeline = harness.service.get_timeline(task_id)
                    answer = "".join(
                        item["text"]
                        for item in timeline["items"]
                        if item.get("run_id") == run["run_id"]
                        and item["kind"] == "text"
                        and item.get("role") == "assistant"
                    )
                    checks = score(step, answer, harness.reads, run)
                    result["steps"].append(
                        {
                            "message": step["message"],
                            "answer": answer,
                            "checks": checks,
                            "run": run,
                            "reads": list(harness.reads),
                            "searches": list(harness.searches),
                        }
                    )
                    if run["status"] != "done":
                        raise RuntimeError("Agent 运行未正常完成，见步骤运行记录")
                # 等本轮后台工作收尾，避免关闭 MCP 时取消仍在执行的记忆判断。
                pending = [
                    *harness.service._active.values(),
                    *harness.service._titles,
                    *harness.service._judges,
                ]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                result["status"] = (
                    "passed"
                    if all(all(step["checks"].values()) for step in result["steps"])
                    else "failed"
                )
        except Exception as exc:
            # 不写异常正文：第三方错误可能包含连接参数；运行证据另行保存。
            result["error"] = type(exc).__name__
        finally:
            try:
                if harness is not None:
                    try:
                        async with asyncio.timeout(20):
                            await harness.stop()
                    finally:
                        if task_id:
                            result["timeline"] = harness.service.get_timeline(task_id)
                            result["observations"] = read_observations(task_id, harness.db_path)
            except Exception as exc:
                result["status"] = "error"
                result["cleanup_error"] = type(exc).__name__
            finally:
                if previous is None:
                    os.environ.pop("PEBBLE_DATA_DIR", None)
                else:
                    os.environ["PEBBLE_DATA_DIR"] = previous
                get_settings.cache_clear()
    result["duration_seconds"] = round(time.monotonic() - started, 2)
    return result


def save_report(path: Path, report: dict) -> None:
    report["summary"] = summarize(report["results"])
    temporary = path / "report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path / "report.json")
    lines = [
        "# Pebble 评测",
        "",
        f"模型：{report['model']}",
        "",
        "真实 Qoder / 本地业务链路；合成资料；Gmail 替身；未验收真实外部写入。",
        "",
        "| 用例 | 次数 | 结果 | 秒 |",
        "| --- | --- | --- | --- |",
    ]
    for result in report["results"]:
        lines.append(
            f"| {result['case_id']} | {result['repeat']} | {result['status']} | "
            f"{result['duration_seconds']} |"
        )
    for result in report["results"]:
        if result["status"] == "passed":
            continue
        lines += ["", f"用例 {result['case_id']} 第 {result['repeat']} 次："]
        for number, step in enumerate(result["steps"], 1):
            failed = [name for name, passed in step["checks"].items() if not passed]
            if failed:
                lines.append(f"- 第 {number} 步未通过：{', '.join(failed)}")
        if result.get("error"):
            lines.append(f"- 运行异常：{result['error']}")
        if result.get("cleanup_error"):
            lines.append(f"- 清理异常：{result['cleanup_error']}")
    lines += [
        "",
        f"统计：`{json.dumps(report['summary'], ensure_ascii=False)}`",
        "",
        "逐步断言、回答、运行轨迹和用量见 report.json。错误计入总尝试，不自动重试。",
    ]
    (path / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args, cases: list[dict]) -> int:
    base = Settings()
    if base.qoder_token is None:
        raise SystemExit("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    model = args.model or base.qoder_model
    if not model or model == "auto":
        raise SystemExit("请用 --model 指定固定型号，或配置 PEBBLE_QODER_MODEL（不接受 auto）")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    report = {
        "model": model,
        "git_commit": git.stdout.strip(),
        "git_status": subprocess.run(
            ["git", "status", "--short"], capture_output=True, text=True
        ).stdout,
        "started_at": datetime.now(UTC).isoformat(),
        "results": [],
        "repeats": args.repeat,
        "timeout_seconds": args.timeout,
    }
    save_report(output, report)
    for case in cases:
        for repeat in range(1, args.repeat + 1):
            print(f"运行 {case['id']} ({repeat}/{args.repeat})", flush=True)
            result = await attempt(case, model, args.timeout)
            result["repeat"] = repeat
            report["results"].append(result)
            save_report(output, report)
            print(f"  {result['status']} ({result['duration_seconds']}s)", flush=True)
    print(f"报告：{output / 'report.md'}")
    return int(any(result["status"] != "passed" for result in report["results"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="列出用例，不调用模型")
    parser.add_argument("--case", action="append", help="只运行指定用例，可重复传入")
    parser.add_argument("--model", help="固定 Qoder 型号")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240, help="每次完整用例的超时秒数")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".eval-results")
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]),
    )
    args = parser.parse_args()
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.case:
        unknown = set(args.case) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"未知用例：{sorted(unknown)}")
        cases = [case for case in cases if case["id"] in args.case]
    if args.repeat < 1 or args.timeout <= 0:
        parser.error("repeat 和 timeout 必须大于 0")
    if args.list:
        for case in cases:
            print(f"{case['id']}: {case['description']}")
        return
    raise SystemExit(asyncio.run(run(args, cases)))


if __name__ == "__main__":
    main()
