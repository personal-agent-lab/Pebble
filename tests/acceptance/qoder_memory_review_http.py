"""真实生产服务的后台记忆回顾端到端验收：HTTP + SSE + SQLite 落库 + 重启恢复。

显式运行，不进入 pytest。直接对仓库实例目录（.data）工作，测试产生的任务最后删除；
长期记忆条目会真实写入实例记忆库。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / ".data" / "pebble.db"

PHASE_A_MESSAGES = [
    "我最近正在学习 Hermes Agent（一个开源个人助理项目）的设计，之后大概会经常问你相关的问题。",
    "另外我更喜欢简洁的中文回复，别每次都写长篇大论。",
    "上次你开头总写“好的！”，这个习惯改掉，直接说结论。",
    "我在准备一个自己的个人助手 side project，存储打算用 SQLite。",
    "就这些，简单确认一下你都知道了。",
]

PHASE_B_MESSAGES = [
    "在吗？想请你以后帮我看材料。",
    "嗯，先这样。",
    "知道了。",
    "行。",
    "好，今天先到这。",
]


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Service:
    def __init__(self, port: int):
        self.port = port
        self.base = f"http://127.0.0.1:{port}/api"
        self.process: subprocess.Popen | None = None

    def start(self):
        env = {**os.environ, "PEBBLE_PORT": str(self.port)}
        log_file = REPO_ROOT / ".data" / "memory-review-http.log"
        with log_file.open("a") as output:
            self.process = subprocess.Popen(
                [
                    "uv",
                    "run",
                    "--project",
                    "server",
                    "python",
                    "-m",
                    "uvicorn",
                    "server.main:create_production_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        wait_for(lambda: self.request("/health")[0] == 200, timeout=60)
        return self

    def stop(self, hard: bool = False):
        if self.process is None:
            return
        if hard:
            self.process.kill()
        else:
            self.process.terminate()
        self.process.communicate(timeout=15)
        self.process = None

    def request(self, path, body=None, method=None, timeout=30, form=False):
        data = (
            None
            if body is None
            else (urlencode(body).encode() if form else json.dumps(body).encode())
        )
        req = Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded" if form else "application/json"
                )
            },
        )
        try:
            with urlopen(req, timeout=timeout) as response:
                if response.status == 204:
                    return response.status, {}
                return response.status, json.load(response)
        except HTTPError as error:
            try:
                return error.code, json.load(error)
            except Exception:
                return error.code, {}
        except URLError:
            return None, {}


def wait_for(predicate, timeout=180, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"等待超时（{timeout}s）")


def query(sql, params=()):
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def review_rows(task_id: str):
    return query(
        "SELECT review_id, status, origin, error FROM memory_reviews "
        "WHERE task_id = ? ORDER BY rowid",
        (task_id,),
    )


def message_run_ids(task_id: str):
    return {
        row["run_id"]
        for row in query(
            "SELECT run_id FROM agent_runs WHERE task_id = ? AND kind = 'message'", (task_id,)
        )
    }


def all_run_kinds(task_id: str):
    rows = query("SELECT DISTINCT kind FROM agent_runs WHERE task_id = ?", (task_id,))
    return {row["kind"] for row in rows}


def timeline_texts(task_id: str):
    return [
        (row["kind"], row["text"])
        for row in query(
            "SELECT kind, text FROM task_timeline_items WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        )
    ]


def is_review_notice(kind, text) -> bool:
    return kind == "notice" and (text or "").startswith("整理记忆：")


def log(step, **fields):
    detail = json.dumps(fields, ensure_ascii=False) if fields else ""
    print(f"[{time.strftime('%H:%M:%S')}] {step} {detail}", flush=True)


class SseListener:
    """持续收集一个任务的全部 SSE 事件，复盘若产生任何事件会落入 collected。"""

    def __init__(self, service: Service, task_id: str):
        self.url = service.base + f"/tasks/{task_id}/events"
        self.collected: list[dict] = []
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with urlopen(self.url, timeout=300) as stream:
                self.connected.set()
                for line in stream:
                    if self._stop.is_set():
                        break
                    if line.startswith(b"data: "):
                        self.collected.append(json.loads(line[6:]))
        except Exception:
            pass

    def start(self):
        self._thread.start()
        assert self.connected.wait(10), "SSE 订阅未连上"
        return self

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)


def send_and_wait(service: Service, task_id: str, message: str):
    status, _ = service.request(f"/tasks/{task_id}/messages", {"message": message}, form=True)
    assert status == 202
    wait_for(
        lambda: service.request(f"/tasks/{task_id}")[1]["latest_run"]["status"] == "done",
        timeout=240,
    )


def phase_a(service: Service) -> dict:
    log("A/5 创建任务并订阅 SSE")
    status, created = service.request(
        "/tasks",
        {"model": os.environ.get("PEBBLE_QODER_MODEL", "auto"), "message": PHASE_A_MESSAGES[0]},
        form=True,
    )
    assert status == 201, created
    task_id = created["task"]["task_id"]
    listener = SseListener(service, task_id).start()
    wait_for(
        lambda: service.request(f"/tasks/{task_id}")[1]["latest_run"]["status"] == "done",
        timeout=240,
    )

    for message in PHASE_A_MESSAGES[1:]:
        send_and_wait(service, task_id, message)
    run_ids = message_run_ids(task_id)
    assert all_run_kinds(task_id) == {"message"}, all_run_kinds(task_id)
    items_after_messages = timeline_texts(task_id)
    user_md_before = (REPO_ROOT / ".data" / "memory" / "USER.md").read_text(encoding="utf-8")

    log("A/5 五轮完成，等待周期复盘执行")

    def review_done():
        rows = review_rows(task_id)
        return bool(rows) and rows[-1]["status"] == "done"

    wait_for(review_done, timeout=300)
    review = review_rows(task_id)[-1]
    assert review["origin"] == "interval", review

    listener.close()
    log("A/5 校验复盘只追加整理提示")
    items_after_review = timeline_texts(task_id)
    assert items_after_review[: len(items_after_messages)] == items_after_messages, (
        "复盘改动了已有时间线"
    )
    # 复盘只可能追加整理提示（修改或删除已有条目时），新增不通知。
    appended = items_after_review[len(items_after_messages) :]
    assert all(is_review_notice(*item) for item in appended), appended
    assert all_run_kinds(task_id) == {"message"}, "复盘产生了新的 agent_runs 行"
    stray = [e for e in listener.collected if e.get("run_id") not in run_ids]
    assert not stray, f"复盘产生了挂在对话轮之外的 SSE 事件：{stray!r}"
    detail = service.request(f"/tasks/{task_id}")[1]
    assert detail["latest_run"]["kind"] == "message", detail["latest_run"]

    user_md = (REPO_ROOT / ".data" / "memory" / "USER.md").read_text(encoding="utf-8")
    assert "Hermes" in user_md, user_md
    memory_changed = user_md != user_md_before
    log(
        "A/5 PASS 周期触发完成，只追加整理提示",
        memory_changed=memory_changed,
        notices=len(appended),
        events=len(listener.collected),
    )

    log("A/5 手动触发与 404")
    status, body = service.request(f"/tasks/{task_id}/memory-review", {})
    assert status == 202, body
    wait_for(
        lambda: len(review_rows(task_id)) == 2 and review_rows(task_id)[-1]["status"] == "done",
        timeout=300,
    )
    assert review_rows(task_id)[-1]["origin"] == "manual"
    assert service.request("/tasks/missing-task/memory-review", {})[0] == 404
    log("A/5 PASS 手动触发 202 + 未知任务 404")

    return {"task_id": task_id, "review": review, "memory_changed": memory_changed}


def phase_b(service: Service) -> str:
    log("B/5 重启恢复：另起任务攒五轮")
    status, created = service.request(
        "/tasks",
        {"model": os.environ.get("PEBBLE_QODER_MODEL", "auto"), "message": PHASE_B_MESSAGES[0]},
        form=True,
    )
    assert status == 201, created
    task_id = created["task"]["task_id"]
    wait_for(
        lambda: service.request(f"/tasks/{task_id}")[1]["latest_run"]["status"] == "done",
        timeout=240,
    )
    for message in PHASE_B_MESSAGES[1:]:
        send_and_wait(service, task_id, message)

    log("B/5 等待复盘进入 running 后强杀进程")

    def review_running():
        rows = review_rows(task_id)
        return bool(rows) and rows[-1]["status"] == "running"

    wait_for(review_running, timeout=120)
    service.stop(hard=True)
    running = review_rows(task_id)[-1]
    assert running["status"] == "running", running

    log("B/5 重启服务，断言复盘被标记中断")
    service.start()
    wait_for(
        lambda: review_rows(task_id)[-1]["status"] == "interrupted",
        timeout=60,
    )
    interrupted = review_rows(task_id)[-1]
    assert interrupted["error"], interrupted
    log("B/5 PASS 中断已落库", review=interrupted)

    log("B/5 第六条消息触发计数自愈")
    send_and_wait(service, task_id, "对了，复盘机制验证得怎么样了？")
    wait_for(
        lambda: (
            review_rows(task_id)[-1]["status"] == "done"
            and review_rows(task_id)[-1]["origin"] == "interval"
        ),
        timeout=300,
    )
    rows = review_rows(task_id)
    assert len(rows) == 2 and rows[-1]["review_id"] != interrupted["review_id"], rows
    assert all_run_kinds(task_id) == {"message"}, "复盘产生了新的 agent_runs 行"
    detail = service.request(f"/tasks/{task_id}")[1]
    assert detail["latest_run"]["kind"] == "message", detail["latest_run"]
    log("B/5 PASS 自愈复盘完成", rows=rows)
    return task_id


def main() -> None:
    port = available_port()
    service = Service(port)
    service.start()
    try:
        report = {"phase_a": phase_a(service), "phase_b_task": phase_b(service)}
        service.request(f"/tasks/{report['phase_a']['task_id']}", method="DELETE")
        service.request(f"/tasks/{report['phase_b_task']}", method="DELETE")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        service.stop()


if __name__ == "__main__":
    main()
