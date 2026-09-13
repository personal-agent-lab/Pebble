"""按七步业务流程自动演示一次：无 pytest、无断言、无真实邮件发送。"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def show(title, value=None):
    print(f"\n[{time.strftime('%H:%M:%S')}] {title}", flush=True)
    if value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def wait_for(read, description, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError(f"等待超时：{description}")


def run():
    root = Path(__file__).resolve().parents[1]
    evidence = Path(tempfile.mkdtemp(prefix="pebble-mail-demo-"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}/api"

    def request(path, body=None, method=None):
        req = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=5) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"HTTP {error.code}: {error.read().decode()}") from error

    show("自动演示：真实 A 后端 + B 替身；所有用户输入自动补齐，不发送真实邮件")
    show("运行资料保存位置", str(evidence))
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PEBBLE_DATA_DIR": str(evidence),
        "PEBBLE_TEST_SEND_STATUS": "sent",
    }
    with (evidence / "backend.log").open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.support.mail_demo:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=root,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:

        def ready():
            if process.poll() is not None:
                raise RuntimeError((evidence / "backend.log").read_text())
            try:
                return request("/health")
            except (URLError, TimeoutError):
                return None

        wait_for(ready, "后端启动")
        from tests.support.mail_demo import MAIL

        show("1/7 收到新邮件：B 替身自动检测并提交；A 去重、创建任务", MAIL)
        task = wait_for(lambda: request("/tasks"), "新邮件任务")[0]
        tid = task["task_id"]
        show("A 返回任务", task)

        def finished(kind=None):
            record = request(f"/tasks/{tid}")
            call = record["latest_run"]
            if call and (kind is None or call["kind"] == kind):
                if call["status"] in {"error", "interrupted"}:
                    raise RuntimeError(f"Agent 调用未完成：{call}")
                if call["status"] == "done":
                    return record
            return None

        wait_for(lambda: finished("new_mail"), "摘要和建议")
        show("2/7 Agent 分析与建议（B 替身）", request(f"/tasks/{tid}/history"))
        message = "帮我写一封回信"
        show("3/7 自动补齐用户选择：只准备草稿，不授权发送", message)
        show("A 接受用户消息", request(f"/tasks/{tid}/messages", {"message": message}))
        wait_for(lambda: finished("message"), "生成草稿")
        operation = request(f"/tasks/{tid}/operations")[0]
        oid = operation["operation_id"]
        draft = request(f"/operations/{oid}/draft")
        show("4/7 B 生成并调用 A 保存，读取完整草稿", draft)
        message = "请再询问一下会议链接"
        show("5/7 自动补齐审核意见", message)
        request(f"/tasks/{tid}/messages", {"message": message})
        wait_for(lambda: finished("message"), "Agent 修改草稿")
        draft = request(f"/operations/{oid}/draft")
        show("Agent 修改后的已保存版本", draft)
        edit = {
            "expected_version": draft["version"],
            "to": ["organizer@example.com"],
            "subject": "回复：周五项目交流会邀请",
            "body": draft["body"] + "\n祝好！",
        }
        show("自动补齐直接编辑：收件人、主题和落款", edit)
        request(f"/operations/{oid}/draft", edit, "PATCH")
        draft = request(f"/operations/{oid}/draft")
        show("用户最终审阅的完整版本", draft)
        show("6/7 自动模拟点击“确认发送”：确认当前版本")
        show(
            "A 接受确认",
            request(
                f"/tasks/{tid}/confirmations",
                {
                    "operation_id": oid,
                    "version": draft["version"],
                },
            ),
        )

        def result():
            value = request(f"/operations/{oid}/execution")
            return value if value["result"] is not None else None

        execution = wait_for(result, "发送结果落盘")
        show("7/7 A 保存的实际结果（发送由 B 替身执行）", execution)
        show(
            "B 发送函数实际收到的参数",
            [json.loads(line) for line in (evidence / "sent.jsonl").read_text().splitlines()],
        )
        wait_for(lambda: finished("execution_result"), "结果回传 Agent 会话")
        show("对应 Agent 会话及后续回复", request(f"/tasks/{tid}/history"))
        show("七步流程演示结束。", str(evidence))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    try:
        run()
    except (OSError, RuntimeError, TimeoutError, KeyError, IndexError) as error:
        show("演示中断", str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
