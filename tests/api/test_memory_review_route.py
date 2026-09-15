"""手动记忆回顾 HTTP 入口：登记、执行与对任务界面的零影响。"""

import time

from fastapi.testclient import TestClient

from server.db import session
from server.main import create_app
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
        return [dict(row) for row in conn.execute("SELECT * FROM memory_reviews ORDER BY rowid")]


def test_manual_review_endpoint_enqueues_and_runs(settings):
    gateway = FakeAgentGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        task_id = client.post("/api/tasks", json={"goal": "闲聊"}).json()["task_id"]
        client.post(
            f"/api/tasks/{task_id}/messages", json={"message": "我最近正在学习 Hermes 的设计"}
        )
        wait_for(
            lambda: client.get(f"/api/tasks/{task_id}").json()["latest_run"]["status"] == "done"
        )
        timeline_before = client.get(f"/api/tasks/{task_id}/timeline").json()["items"]

        response = client.post(f"/api/tasks/{task_id}/memory-review")
        assert response.status_code == 202
        row = response.json()
        assert (row["status"], row["origin"]) == ("pending", "manual")

        wait_for(lambda: review_rows() and review_rows()[0]["status"] == "done")
        (call,) = gateway.review_calls
        assert call["task_id"] == task_id
        assert "我最近正在学习 Hermes 的设计" in call["transcript"]

        # 回顾不进入任务界面：时间线与最新调用都不变。
        assert client.get(f"/api/tasks/{task_id}/timeline").json()["items"] == timeline_before
        latest = client.get(f"/api/tasks/{task_id}").json()["latest_run"]
        assert (latest["kind"], latest["status"]) == ("message", "done")


def test_manual_review_endpoint_rejects_unknown_task(settings):
    with TestClient(create_app(gateway=FakeAgentGateway())) as client:
        response = client.post("/api/tasks/missing/memory-review")
        assert response.status_code == 404


def test_interval_review_triggers_after_five_messages_over_http(settings):
    gateway = FakeAgentGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        task_id = client.post("/api/tasks", json={"goal": "闲聊"}).json()["task_id"]
        for index in range(5):
            client.post(f"/api/tasks/{task_id}/messages", json={"message": f"第 {index + 1} 句"})
            wait_for(
                lambda: client.get(f"/api/tasks/{task_id}").json()["latest_run"]["status"]
                == "done"
            )
        wait_for(lambda: review_rows() and review_rows()[0]["status"] == "done")
        assert len(gateway.review_calls) == 1
        assert gateway.review_calls[0]["transcript"].count("用户：") == 5
        assert review_rows()[0]["origin"] == "interval"
