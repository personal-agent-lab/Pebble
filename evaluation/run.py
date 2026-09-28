"""评测入口。

用法（仓库根执行，消耗真实模型额度）：

    uv run --project server python -m evaluation.run --scenario S1 [--repeat N] [--model ID]

S 层无需外部凭证；M/C/F/L 层需要专用测试账号，接入前不注册进登记表。
每次重复使用独立实例与数据目录，产物落在 .eval-results/（数据目录保留作证据）。
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from .harness.config import EvalSettings
from .harness.model import Scenario, ScenarioContext
from .harness.report import ScenarioRecord, write_report
from .harness.seeds import SeedBuilder
from .harness.server import InstanceServer
from .scenarios import REGISTRY


def _run_repetition(
    scenario: Scenario, repetition: int, settings: EvalSettings, rep_dir: Path
) -> ScenarioRecord:
    record = ScenarioRecord(
        scenario_id=scenario.id,
        title=scenario.title,
        layer=scenario.layer,
        repetition=repetition,
        duration_s=0.0,
    )
    started = time.monotonic()
    server = InstanceServer(settings, rep_dir)
    try:
        # 种子必须在实例启动前铺好：数据目录是初始状态的一部分。
        rep_dir.mkdir(parents=True, exist_ok=True)
        scenario.seed(SeedBuilder(server.data_dir))
        server.start()
        ctx = ScenarioContext(client=server.client, settings=settings, data_dir=server.data_dir)
        record.assertions = scenario.run(ctx)
        record.evidence = ctx.evidence
    except BaseException as exc:  # noqa: BLE001 任何失败都如实入报告
        record.error = f"{exc!r}\n{traceback.format_exc()}"
    finally:
        server.stop()
    record.duration_s = time.monotonic() - started
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pebble 真实场景评测")
    parser.add_argument(
        "--scenario", default="S1", help=f"场景 id 或 all；可选：{','.join(REGISTRY)}"
    )
    parser.add_argument("--repeat", type=int, default=None, help="覆盖场景默认重复次数")
    parser.add_argument("--model", default="", help="覆盖主模型（默认 auto）")
    args = parser.parse_args(argv)

    settings = EvalSettings(model=args.model)
    selected = (
        list(REGISTRY.values()) if args.scenario == "all" else [REGISTRY[args.scenario]]
    )
    started_at = datetime.now(UTC)
    run_dir = settings.results_root / started_at.strftime("%Y%m%d-%H%M%S")
    records: list[ScenarioRecord] = []
    for scenario in selected:
        repeat = args.repeat if args.repeat is not None else scenario.repeat
        for repetition in range(1, repeat + 1):
            rep_dir = run_dir / f"{scenario.id}-r{repetition}"
            print(f"[run] {scenario.id} {scenario.title} 第 {repetition}/{repeat} 次…", flush=True)
            record = _run_repetition(scenario, repetition, settings, rep_dir)
            records.append(record)
            if record.passed:
                verdict = "通过"
            elif record.pending_reviews and len(record.failed) == len(record.pending_reviews):
                verdict = "待复核"
            else:
                verdict = "失败"
            print(
                f"[run] {scenario.id} r{repetition} {verdict}"
                f"（断言 {len(record.assertions)}，失败 {len(record.failed)}）",
                flush=True,
            )

    report_path = write_report(
        run_dir,
        records,
        meta={
            "started_at": started_at.isoformat(),
            "model": settings.effective_model,
            "scenarios": [s.id for s in selected],
        },
    )
    violations = [a for r in records for a in r.red_line_violations]
    infra_failures = [a for r in records for a in r.infra_failures]
    passed = sum(1 for r in records if r.passed)
    print(
        f"\n报告：{report_path}"
        f"（通过 {passed}/{len(records)}，红线违规 {len(violations)}，"
        f"取证自检异常 {len(infra_failures)}（不计分））"
    )
    return 0 if passed == len(records) and not violations else 1


if __name__ == "__main__":
    sys.exit(main())
