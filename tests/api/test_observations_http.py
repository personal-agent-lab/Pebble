"""观测读取接口：404、503 与按轮次顺序的只读响应。"""

import json
import sqlite3
from typing import Any

from fastapi.testclient import TestClient

import server.api.routes as routes
from server.db import session, write
from server.main import create_app
from server.sessions.service import SessionStore


def client_for() -> TestClient:
    return TestClient(create_app(gateway=None))


def seed_observed_run(task_id: str, run_id: str, *, sdk_result: str | None = None) -> None:
    """直接落一条已结束的轮次与观测数据，读取接口不该关心写入来源。"""
    with session() as conn, write(conn):
        conn.execute(
            "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at, "
            "started_at, finished_at) VALUES (?, ?, 'message', '{}', 'done', 't0', 't1', 't2')",
            (run_id, task_id),
        )
        conn.execute(
            "INSERT INTO run_observations (run_id, materials, sdk_result, updated_at) "
            "VALUES (?, ?, ?, 't2')",
            (
                run_id,
                json.dumps({"assembled": [{"title": "关于你", "chars": 10}], "skipped": []}),
                sdk_result,
            ),
        )
        conn.execute(
            "INSERT INTO observation_steps (step_id, run_id, kind, code, status, started_at, "
            "ended_at, item_id, tool_call_id, detail) VALUES "
            "('s1', ?, 'tool', 'gmail_search', 'ok', 't1', 't2', 'i1', 'c1', ?)",
            (run_id, json.dumps({"source": "mcp", "chars": 42})),
        )


def test_observations_endpoint_returns_runs_and_steps(settings: Any) -> None:
    with client_for() as client:
        task_id = SessionStore().create_task("看观测")["task_id"]
        seed_observed_run(task_id, "run-1")

        response = client.get(f"/api/tasks/{task_id}/observations")

        assert response.status_code == 200
        runs = response.json()["runs"]
        assert len(runs) == 1
        observed = runs[0]
        assert observed["run_id"] == "run-1"
        assert observed["status"] == "done" and observed["kind"] == "message"
        assert observed["materials"]["assembled"] == [{"title": "关于你", "chars": 10}]
        step = observed["steps"][0]
        assert step["code"] == "gmail_search" and step["status"] == "ok"
        assert step["item_id"] == "i1" and step["detail"]["chars"] == 42


def test_observations_endpoint_derives_and_labels_turn_totals(settings: Any) -> None:
    with client_for() as client:
        task_id = SessionStore().create_task("推算合计")["task_id"]
        seed_observed_run(
            task_id,
            "run-1",
            sdk_result=json.dumps(
                {
                    "duration_ms": 5,
                    "usage": [{"message_id": "m1", "input_tokens": 0, "output_tokens": 0}],
                    "session_totals": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "credits": 1.0,
                    },
                }
            ),
        )

        runs = client.get(f"/api/tasks/{task_id}/observations").json()["runs"]

        # 合计来自会话累计快照的差值，来源标注为 delta；逐次条目照原样返回。
        assert runs[0]["usage_totals"] == {
            "input_tokens": 100,
            "output_tokens": 20,
            "credits": 1.0,
        }
        assert runs[0]["usage_totals_source"] == "delta"
        assert runs[0]["sdk_result"]["session_totals"]["credits"] == 1.0


def test_observations_endpoint_returns_unobserved_run_with_nulls(settings: Any) -> None:
    with client_for() as client:
        task_id = SessionStore().create_task("空观测")["task_id"]
        seed_observed_run(task_id, "run-1")
        with session() as conn, write(conn):
            conn.execute(
                "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at) "
                "VALUES ('run-0', ?, 'message', '{}', 'pending', 't0')",
                (task_id,),
            )

        runs = client.get(f"/api/tasks/{task_id}/observations").json()["runs"]

        # 按 agent_runs 顺序（插入先后）排列；未观测到的轮次字段为空值。
        assert [run["run_id"] for run in runs] == ["run-1", "run-0"]
        assert runs[1]["materials"] is None and runs[1]["steps"] == []
        assert runs[1]["usage_totals"]["credits"] is None


def test_observations_missing_task_returns_404(settings: Any) -> None:
    with client_for() as client:
        response = client.get("/api/tasks/does-not-exist/observations")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"


def test_observations_store_failure_returns_503(settings, monkeypatch) -> None:
    with client_for() as client:
        task_id = SessionStore().create_task("观测")["task_id"]

        def broken(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(routes, "read_observations", broken)
        response = client.get(f"/api/tasks/{task_id}/observations")
        assert response.status_code == 503
        assert response.json()["error"] == "unavailable"
