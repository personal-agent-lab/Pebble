"""技能复盘手动触发 HTTP 入口：登记、后台执行与未接入时的显式失败。"""

import time

from fastapi.testclient import TestClient

from server.db import session
from server.main import create_app
from server.skills.service import SkillService
from tests.support.agent_double import FakeAgentGateway


def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待服务超时")


def review_rows() -> list[dict]:
    with session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM skill_reviews ORDER BY rowid")]


def test_manual_skill_review_endpoint_enqueues_and_runs(settings):
    gateway = FakeAgentGateway()
    with TestClient(
        create_app(gateway=gateway, skills=SkillService(settings.data_dir, settings.db_path))
    ) as client:
        created = client.post(
            "/api/tasks",
            data={"model": "qmodel_38max", "message": "纠正一下周报的分节顺序"},
        )
        task_id = created.json()["task"]["task_id"]
        wait_for(
            lambda: client.get(f"/api/tasks/{task_id}").json()["latest_run"]["status"] == "done"
        )

        response = client.post("/api/skills/review")
        assert response.status_code == 202
        row = response.json()
        assert (row["status"], row["origin"]) == ("pending", "manual")

        # 默认间隔 10 轮，这一轮只能来自手动触发。
        wait_for(lambda: review_rows() and review_rows()[0]["status"] == "completed")
        (call,) = gateway.skill_review_calls
        assert "纠正一下周报的分节顺序" in call["material"]


def test_manual_skill_review_endpoint_reports_unconfigured(settings):
    with TestClient(create_app(gateway=FakeAgentGateway())) as client:
        response = client.post("/api/skills/review")
        assert response.status_code == 503
