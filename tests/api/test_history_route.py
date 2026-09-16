"""页面历史搜索接口：与 Agent 工具同一套检索，不排除任何任务，输入不合法返回 422。"""

import time

from fastapi.testclient import TestClient

from server.main import create_app
from tests.support.agent_double import FakeAgentGateway


def wait_done(client, task_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = client.get(f"/api/tasks/{task_id}").json()["latest_run"]
        if run and run["status"] == "done":
            return
        time.sleep(0.02)
    raise AssertionError("等待调用结束超时")


def test_search_history_over_http(settings):
    with TestClient(create_app(gateway=FakeAgentGateway())) as client:
        task_id = client.post("/api/tasks", json={"goal": "预算"}).json()["task_id"]
        client.post(f"/api/tasks/{task_id}/messages", json={"message": "运维预算提高一成"})
        wait_done(client, task_id)

        results = client.get("/api/history/search", params={"q": "运维预算"}).json()["results"]
        assert {item["speaker"] for item in results} == {"user", "assistant"}
        assert all(item["task_id"] == task_id for item in results)
        assert results[0]["task_title"]

        invalid = client.get("/api/history/search", params={"q": "预算", "after": "昨天"})
        assert invalid.status_code == 422
        assert invalid.json()["error"] == "invalid_history"
