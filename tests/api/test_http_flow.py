"""真实 HTTP/SSE 与独立进程验收；外部 Agent/Gmail 仅使用测试替身。"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
from fastapi.testclient import TestClient

from server.db import session
from server.main import create_app
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待服务超时")


def log(step, **fields):
    detail = json.dumps(fields, ensure_ascii=False) if fields else ""
    print(f"[{time.strftime('%H:%M:%S')}] {step} {detail}", flush=True)


@pytest.mark.parametrize("outcome", ["sent", "failed", "unknown"])
def test_http_sse_flow_and_process_restart(settings, outcome, monkeypatch):
    monkeypatch.setenv("PEBBLE_TEST_SEND_STATUS", outcome)
    log("开始：真实 A 后端 + B 替身", outcome=outcome, evidence=str(settings.data_dir))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}/api"

    def request(path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(
            base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def ready():
        try:
            return request("/health")[0] == 200
        except (URLError, TimeoutError):
            return False

    def start():
        env = {
            **os.environ,
            "PYTHONPATH": str(Path.cwd()),
        }
        with (settings.data_dir / "backend.log").open("a") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "tests.support.backend_fixture:app",
                    "--port",
                    str(port),
                ],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            wait_for(ready)
        except BaseException:
            process.terminate()
            process.communicate(timeout=10)
            raise AssertionError((settings.data_dir / "backend.log").read_text()) from None
        return process

    process = start()
    try:
        log("1/8 后端已启动；重复提交同一新邮件")
        task = wait_for(lambda: request("/tasks")[1])[0]
        tid = task["task_id"]
        wait_for(lambda: request(f"/tasks/{tid}")[1]["latest_run"]["status"] == "done")
        assert len(request("/tasks")[1]) == 1
        with session() as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM agent_runs WHERE kind = 'new_mail'").fetchone()[
                    0
                ]
                == 1
            )
        log("PASS 新邮件去重", task_id=tid)
        assert request(f"/tasks/{tid}/operations")[1] == []
        assert not (settings.data_dir / "sent.jsonl").exists()
        assert "摘要与建议" in request(f"/tasks/{tid}/history")[1]["messages"][0]["text"]

        log(
            "2/8 PASS 摘要和建议可通过接口读取，草稿=0，发送=0",
            history=request(f"/tasks/{tid}/history")[1],
        )

        # 建立真实 SSE 连接，收到第一条事件后关闭；后台应继续保存草稿。
        connected = threading.Event()
        received = []

        def subscribe():
            with urlopen(base + f"/tasks/{tid}/events", timeout=5) as stream:
                connected.set()
                for line in stream:
                    if line.startswith(b"data: "):
                        received.append(json.loads(line[6:]))
                        break

        listener = threading.Thread(target=subscribe)
        listener.start()
        assert connected.wait(5)
        log("3/8 用户要求准备回信，不授权发送")
        assert request(f"/tasks/{tid}/messages", {"message": "帮我写一封回信"})[0] == 202
        listener.join(5)
        assert received and not listener.is_alive()
        wait_for(lambda: request(f"/tasks/{tid}")[1]["latest_run"]["status"] == "done")
        oid = request(f"/tasks/{tid}/operations")[1][0]["operation_id"]
        assert not (settings.data_dir / "sent.jsonl").exists()
        with session() as conn:
            assert conn.execute("SELECT COUNT(*) FROM approval_executions").fetchone()[0] == 0
        log(
            "4/8 PASS SSE 已断开，后台仍完成草稿；无确认、无发送",
            event=received,
            draft=request(f"/operations/{oid}/draft")[1],
        )
        assert request(f"/tasks/{tid}/messages", {"message": "第二稿：请写得更正式"})[0] == 202
        wait_for(lambda: request(f"/tasks/{tid}")[1]["latest_run"]["status"] == "done")
        assert request(f"/tasks/{tid}/messages", {"message": "第三稿：请再简短一些"})[0] == 202
        wait_for(lambda: request(f"/tasks/{tid}")[1]["latest_run"]["status"] == "done")
        log("5/8 Agent 已完成两轮修改，接下来模拟网页直接编辑")
        final = {
            "to": ["b@example.com", "a@example.com"],
            "subject": " 最终主题 ",
            "body": "你好，\n\n  我参加。\n",
        }
        assert (
            request(f"/operations/{oid}/draft", {"expected_version": 3, **final}, "PATCH")[1][
                "version"
            ]
            == 4
        )
        for version, body in enumerate(
            ["帮我写一封回信", "第二稿：请写得更正式", "第三稿：请再简短一些", final["body"]], 1
        ):
            assert request(f"/operations/{oid}/draft?version={version}")[1]["body"] == body
        assert not (settings.data_dir / "sent.jsonl").exists()
        log("PASS 四个版本均可读取，发送=0", draft=request(f"/operations/{oid}/draft")[1])
        status, error = request(f"/tasks/{tid}/confirmations", {"operation_id": oid, "version": 1})
        assert status == 409 and error["current_version"] == 4
        assert not (settings.data_dir / "sent.jsonl").exists()
        log("6/8 PASS 确认旧版本被拒绝，发送=0", response=error)
        log("确认最终版本 v4，并重复提交同一确认")
        for _ in range(2):
            assert (
                request(f"/tasks/{tid}/confirmations", {"operation_id": oid, "version": 4})[0]
                == 202
            )
        execution = wait_for(
            lambda: (
                value
                if (value := request(f"/operations/{oid}/execution")[1])["status"] == outcome
                else None
            )
        )
        wait_for(lambda: request(f"/tasks/{tid}")[1]["latest_run"]["status"] == "done")
        sent = [
            json.loads(line) for line in (settings.data_dir / "sent.jsonl").read_text().splitlines()
        ]
        assert len(sent) == 1
        assert sent[0] == {
            "operation_id": oid,
            "version": 4,
            "source_message_id": "fixture-mail",
            "thread_id": "fixture-thread",
            **final,
        }
        assert execution["confirmation"]["task_id"] == tid
        with session() as conn:
            delivery = conn.execute(
                "SELECT * FROM agent_runs WHERE kind = 'execution_result'"
            ).fetchall()
            assert (
                len(delivery) == 1
                and delivery[0]["task_id"] == tid
                and delivery[0]["status"] == "done"
            )
        log("7/8 PASS 发送仅调用一次，参数逐字段一致，结果回传完成", execution=execution)
        history = request(f"/tasks/{tid}/history")[1]["messages"]
        assert any(f"发送结果：{outcome}" in row["text"] for row in history)
        log("PASS 对应会话后续回复", history=history)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    log("8/8 重启后端，验证持久化和禁止重发")
    process = start()
    try:
        assert len(request("/tasks")[1]) == 1
        assert request(f"/operations/{oid}/execution")[1] == execution
        assert request(f"/operations/{oid}/draft")[1]["body"] == final["body"]
        assert request(f"/tasks/{tid}/confirmations", {"operation_id": oid, "version": 4})[0] == 202
        wait_for(lambda: request(f"/operations/{oid}/execution")[1] == execution)
        assert len((settings.data_dir / "sent.jsonl").read_text().splitlines()) == 1
        with session() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE kind = 'execution_result'"
                ).fetchone()[0]
                == 1
            )
        log("PASS 重启后草稿和结果一致；重复确认未重发", outcome=outcome)
        log("场景验收通过", outcome=outcome)
    finally:
        process.terminate()
        process.communicate(timeout=10)


def test_missing_dependencies_reject_before_mutation(settings):
    with TestClient(create_app()) as client:
        tid = client.post("/api/tasks", json={"goal": "测试"}).json()["task_id"]
        assert (
            client.post(f"/api/tasks/{tid}/messages", json={"message": "你好"}).status_code == 503
        )
        drafts = ReplyDraftStore(lambda **fields: {"valid": True, "errors": []})
        op = drafts.save_reply_draft(tid, "m", "thread", ["a@example.com"], "主题", "正文")
        assert (
            client.post(
                f"/api/tasks/{tid}/confirmations",
                json={"operation_id": op["operation_id"], "version": 1},
            ).status_code
            == 503
        )
        assert drafts.get_reply_draft(op["operation_id"])["status"] == "pending"
        with session() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM approval_executions").fetchone()[0] == 0
        assert len(SessionStore().list_tasks()) == 1
