"""模型固定与附件 multipart 链路的 HTTP 验收。"""

import asyncio
import time

from fastapi.testclient import TestClient

from server.agent.models import ModelEntry
from server.errors import ModelValidationError
from server.main import create_app
from tests.support.agent_double import FakeAgentGateway


class StaticCatalog:
    default_model = "model-a"

    def __init__(self):
        self.available = {"model-a", "custom-id"}

    async def list_models(self, *, fresh=True):
        return [
            ModelEntry("model-a", "Model A", "managed"),
            ModelEntry("custom-id", "私有模型", "custom"),
        ]

    async def response(self):
        return {
            "default_model": self.default_model,
            "models": [entry.response() for entry in await self.list_models()],
        }

    def refresh_in_background(self):
        pass

    async def close(self):
        pass

    async def validate(self, model, *, fresh=True):
        if model not in self.available:
            raise ModelValidationError(model)
        return model


def wait_done(client, task_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        latest = client.get(f"/api/tasks/{task_id}").json()["latest_run"]
        if latest and latest["status"] in {"done", "error"}:
            return latest
        time.sleep(0.02)
    raise AssertionError("等待调用结束超时")


def test_catalog_create_and_attachment_round_trip(settings):
    gateway = FakeAgentGateway()
    catalog = StaticCatalog()
    app = create_app(gateway=gateway, model_catalog=catalog)

    with TestClient(app) as client:
        assert client.get("/api/models").json() == asyncio.run(catalog.response())
        response = client.post(
            "/api/tasks",
            data={"model": "custom-id", "message": "分析这些材料"},
            files=[
                ("files", ("pixel.png", b"\x89PNG\r\n\x1a\nrandom", "image/png")),
                ("files", ("notes.md", "随机内容-甲".encode(), "text/markdown")),
            ],
        )
        assert response.status_code == 201, response.text
        task = response.json()["task"]
        task_id = task["task_id"]
        assert task["model"] == "custom-id"
        assert wait_done(client, task_id)["status"] == "done"

        (call,) = gateway.calls_of("message")
        assert call["model"] == "custom-id"
        assert [item.filename for item in call["attachments"]] == ["pixel.png", "notes.md"]
        assert all(item.path.is_file() for item in call["attachments"])
        # Runtime 的 Read 按扩展名识别 PDF 与图片，落盘名必须保留白名单后缀。
        assert [item.path.suffix for item in call["attachments"]] == [".png", ".md"]

        items = client.get(f"/api/tasks/{task_id}/timeline").json()["items"]
        user = next(item for item in items if item.get("role") == "user")
        assert user["text"] == "分析这些材料"
        assert [item["filename"] for item in user["attachments"]] == ["pixel.png", "notes.md"]
        image, markdown = user["attachments"]
        assert client.get(image["url"]).content == b"\x89PNG\r\n\x1a\nrandom"
        download = client.get(markdown["url"])
        assert download.content == "随机内容-甲".encode()
        assert "attachment" in download.headers["content-disposition"]

        other = app.state.tasks.create_task("另一个任务", model="model-a")
        assert (
            client.get(f"/api/tasks/{other['task_id']}/attachments/{image['file_id']}").status_code
            == 404
        )


def test_attachment_only_and_delete_cleanup(settings):
    gateway = FakeAgentGateway()
    app = create_app(gateway=gateway, model_catalog=StaticCatalog())
    with TestClient(app) as client:
        response = client.post(
            "/api/tasks",
            data={"model": "model-a"},
            files={"files": ("only.txt", b"standalone", "text/plain")},
        )
        assert response.status_code == 201
        task_id = response.json()["task"]["task_id"]
        assert wait_done(client, task_id)["status"] == "done"
        assert gateway.calls_of("message")[0]["message"] == "请阅读并处理本轮附件。"
        user = client.get(f"/api/tasks/{task_id}/timeline").json()["items"][0]
        assert user["text"] == ""
        assert user["attachments"][0]["filename"] == "only.txt"

        workspace = app.state.attachments.task_workspace(task_id)
        assert workspace.is_dir()
        assert client.delete(f"/api/tasks/{task_id}").status_code == 204
        assert not workspace.exists()


def test_rejects_invalid_model_files_and_entire_batch(settings):
    app = create_app(gateway=FakeAgentGateway(), model_catalog=StaticCatalog())
    with TestClient(app) as client:
        unknown = client.post("/api/tasks", data={"model": "gone", "message": "hello"})
        assert unknown.status_code == 422
        assert unknown.json()["error"] == "invalid_model"
        assert client.get("/api/tasks").json() == []

        forged = client.post(
            "/api/tasks",
            data={"model": "model-a"},
            files={"files": ("fake.png", b"plain text", "image/png")},
        )
        assert forged.status_code == 422
        assert forged.json()["errors"][0]["field"] == "fake.png"

        batch = client.post(
            "/api/tasks",
            data={"model": "model-a"},
            files=[
                ("files", ("valid.txt", b"valid", "text/plain")),
                ("files", ("archive.zip", b"PK", "application/zip")),
            ],
        )
        assert batch.status_code == 422
        assert client.get("/api/tasks").json() == []


def test_message_limits_and_model_disappearing_fail_the_run(settings):
    catalog = StaticCatalog()
    app = create_app(gateway=FakeAgentGateway(), model_catalog=catalog)
    with TestClient(app) as client:
        task_id = app.state.tasks.create_task("固定模型", model="custom-id")["task_id"]
        too_many = client.post(
            f"/api/tasks/{task_id}/messages",
            files=[("files", (f"{index}.txt", b"x", "text/plain")) for index in range(11)],
        )
        assert too_many.status_code == 422

        too_large = client.post(
            f"/api/tasks/{task_id}/messages",
            files={"files": ("large.txt", b"x" * (20 * 1024 * 1024 + 1), "text/plain")},
        )
        assert too_large.status_code == 422

        catalog.available.remove("custom-id")
        accepted = client.post(f"/api/tasks/{task_id}/messages", data={"message": "继续"})
        assert accepted.status_code == 202
        assert wait_done(client, task_id)["status"] == "error"
        timeline = client.get(f"/api/tasks/{task_id}/timeline").json()["items"]
        assert any("模型当前不可用：custom-id" in item.get("text", "") for item in timeline)
