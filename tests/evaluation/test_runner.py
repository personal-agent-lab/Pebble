"""评分必须拒绝只猜中答案、旧答案混入和运行失败；汇总不能隐藏服务异常。"""

from tests.evaluation.__main__ import save_report, score, summarize


def test_answer_alone_cannot_pass_and_stale_fact_is_rejected():
    step = {"contains": ["NEW"], "excludes": ["OLD"], "read_paths": ["a.md"]}
    assert not all(score(step, "NEW", [], {"status": "done"}).values())
    checks = score(step, "NEW OLD", ["ref:a.md"], {"status": "done"})
    assert not checks["no_stale_or_unrelated_facts"]
    assert not all(score(step, "NEW", ["ref:a.md"], {"status": "error"}).values())
    assert all(score(step, "NEW", ["ref:a.md"], {"status": "done"}).values())
    assert all(score(step, "NEW", ["document:kb/a.md"], {"status": "done"}).values())


def test_errors_remain_in_denominator_and_report(tmp_path):
    results = [
        {"case_id": "a", "status": "passed", "repeat": 1, "duration_seconds": 1},
        {"case_id": "a", "status": "error", "repeat": 2, "duration_seconds": 1},
        {"case_id": "b", "status": "failed", "repeat": 1, "duration_seconds": 1},
    ]
    for result in results:
        result["steps"] = []
    totals = summarize(results)
    assert totals == {
        "attempts": 3,
        "passed": 1,
        "failed": 1,
        "errors": 1,
        "cases_all_attempts_passed": 0,
        "cases": 2,
    }
    save_report(tmp_path, {"model": "test", "results": results})
    assert '"status": "error"' in (tmp_path / "report.json").read_text()
    assert "| a | 2 | error |" in (tmp_path / "report.md").read_text()


def test_setup_failure_is_reported_and_restores_instance(monkeypatch, tmp_path):
    import asyncio
    import os
    from pathlib import Path

    from tests.evaluation import __main__ as runner

    instance = tmp_path / "real-instance"
    monkeypatch.setenv("PEBBLE_DATA_DIR", str(instance))
    roots = []
    closed = []

    class BrokenHarness:
        def __init__(self, root, settings, port):
            assert root != instance
            assert Path(os.environ["PEBBLE_DATA_DIR"]) == settings.data_dir == root
            roots.append(root)

        async def start(self):
            raise OSError("fixture unavailable")

        async def stop(self):
            closed.append(True)

    monkeypatch.setattr(runner, "Harness", BrokenHarness)
    result = asyncio.run(runner.attempt({"id": "broken"}, "test-model", 1))
    assert result["status"] == "error" and result["error"] == "OSError"
    assert closed == [True]
    assert os.environ["PEBBLE_DATA_DIR"] == str(instance)
    assert not roots[0].exists()
    assert not instance.exists()
