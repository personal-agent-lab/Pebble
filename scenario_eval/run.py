"""可替换场景的闭环运行器；不依据内容或世界状态产生验收结论。"""

import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

from scenario_eval.api import ApiError, PebbleClient
from scenario_eval.models import REVIEW, USER, call, resolve_models
from scenario_eval.world import Clock, save

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def files(root):
    return {
        str(p.relative_to(root)): p.read_text() for p in root.rglob("*.md") if ".git" not in p.parts
    }


def visible(items):
    # 用户看到正文、通知、错误、卡片；不暴露工具返回或内部观测。
    return [item for item in items if item.get("kind") in {"text", "notice", "error", "mail_draft"}]


def database(path):
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = [
            "tasks",
            "agent_runs",
            "operations",
            "task_operations",
            "mail_drafts",
            "mail_draft_versions",
            "approval_executions",
            "run_observations",
        ]
        return {
            name: [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
            for name in tables
            if name in names
        }


class Runner:
    def __init__(self, api, directory, agent_model):
        self.api, self.directory, self.agent_model = api, directory, agent_model
        self.clock = Clock(directory / "clock.json")
        self.records, self.tasks = [], []

    def record(self, kind, **fields):
        event = {
            "index": len(self.records),
            "at": self.clock.now().isoformat(),
            "kind": kind,
            **fields,
        }
        self.records.append(event)
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def snapshot(self, task):
        self.record(
            "snapshot", task_id=task, task=self.api.get_task(task), timeline=self.api.timeline(task)
        )

    def wait_turn(self, task, run):
        result = self.api.wait_turn(task, run["run_id"], timeout=480, poll_interval=1)
        self.record("turn_end", task_id=task, run=result)
        self.snapshot(task)

    def context(self):
        return {
            "now": self.clock.now().isoformat(),
            "tasks": [
                {"task_id": t, "timeline": visible(self.api.timeline(t))} for t in self.tasks
            ],
            "actions_and_results": [
                e
                for e in self.records
                if e["kind"] in {"action", "action_result", "action_error", "time_advanced"}
            ],
        }

    def execute(self, action):
        self.record("action", decision=action)
        kind = action.get("action")
        if kind not in {"say", "start_task", "edit_card", "confirm", "cancel", "wait", "finish"}:
            raise ValueError("未知用户动作")
        if kind == "finish":
            return False
        if kind == "wait":
            until = datetime.fromisoformat(action["until"])
            if until.tzinfo is None or until <= self.clock.now():
                raise ValueError("等待目标必须是带时区的未来时间")
            self.clock.set(until.isoformat())
            self.record("time_advanced", mode="controlled_server_clock", until=until.isoformat())
            return True
        if kind == "start_task":
            payload = self.api.create_task(message=action["message"], model=self.agent_model)
            task = payload["task"]["task_id"]
            self.tasks.append(task)
            self.wait_turn(task, payload["run"])
            return True
        task = action["task_id"]
        if task not in self.tasks:
            raise ValueError("动作指向非本次评测任务")
        if kind == "say":
            target = None
            if action.get("operation_id"):
                target = {"kind": "mail_draft", "operation_id": action["operation_id"]}
            run = self.api.send_message(task, message=action["message"], target=target)
            self.wait_turn(task, run)
            return True
        draft = self.api.get_draft(action["operation_id"])
        # 验证卡片归属是动作能力边界，不是验收断言。
        cards = [
            i["draft"]["operation_id"]
            for i in self.api.timeline(task)
            if i.get("kind") == "mail_draft"
        ]
        if draft["operation_id"] not in cards:
            raise ValueError("卡片不属于指定任务")
        if kind == "edit_card":
            result = self.api.patch_draft(
                draft["operation_id"],
                expected_version=action["version"],
                body=action["body"],
                to=draft["to"],
                subject=draft["subject"],
            )
        elif kind == "cancel":
            result = self.api.cancel(task, draft["operation_id"], action["version"])
        else:
            # 不修改模型决定的版本；产品确认服务自行验证。请求已记录，不自动重发。
            previous_run = self.api.get_task(task).get("latest_run")
            previous_run_id = previous_run["run_id"] if previous_run else None
            result = self.api.confirm(task, draft["operation_id"], action["version"])
        self.record("action_result", action=kind, result=result)
        if kind == "confirm":
            deadline = time.monotonic() + 480
            while time.monotonic() < deadline:
                run = self.api.get_task(task).get("latest_run")
                if (
                    run
                    and run.get("kind") == "execution_result"
                    and run["run_id"] != previous_run_id
                ):
                    self.wait_turn(task, run)
                    break
                time.sleep(1)
            else:
                raise TimeoutError("执行回报等待超时；不自动重发")
        else:
            self.snapshot(task)
        return True

    def evidence(self, initial):
        return {
            "initial": initial,
            "events": self.records,
            "tasks": [
                {
                    "task": self.api.get_task(t),
                    "timeline": self.api.timeline(t),
                    "observations": self.api.observations(t),
                }
                for t in self.tasks
            ],
            "mail": json.loads((self.directory / "mail.json").read_text()),
            "calendar": json.loads((self.directory / "calendar.json").read_text()),
            "files": files(self.directory / "data"),
            "database": database(self.directory / "data" / "pebble.db"),
            "time_mode": (
                "controlled_server_clock; SDK host date unchanged; "
                "current controlled date supplied as query context"
            ),
        }


def run(args):
    scenario = args.scenario.resolve()
    directory = args.output.resolve()
    # 输出含实例数据，只能落在已忽略目录，且不覆盖已有运行。
    result = subprocess.run(["git", "check-ignore", "-q", str(directory)], cwd=ROOT)
    if result.returncode != 0:
        raise ValueError("输出必须位于Git忽略目录，例如 .eval-results/")
    args.agent_model, args.user_model, args.review_model = asyncio.run(
        asyncio.wait_for(resolve_models([args.agent_model, args.user_model, args.review_model]), 90)
    )
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copytree(scenario, directory / "scenario")
    scenario = directory / "scenario"
    world = json.loads((directory / "scenario" / "world.json").read_text())
    seeds = scenario / "seeds"
    if seeds.exists():
        shutil.copytree(seeds, directory / "data")
    else:
        (directory / "data").mkdir()
    Clock(directory / "clock.json").set(world["start_at"])
    save(
        directory / "manifest.json",
        {
            "scenario": scenario.name,
            "agent_model": args.agent_model,
            "user_model": args.user_model,
            "review_model": args.review_model,
            "max_actions": args.max_actions,
        },
    )
    initial = {"world": world, "files": files(directory / "data")}
    save(directory / "initial.json", initial)
    service_port, tool_port = port(), port()
    env = {
        **os.environ,
        "PEBBLE_EVAL_RUN_DIR": str(directory),
        "PEBBLE_DATA_DIR": str(directory / "data"),
        "PEBBLE_AUTH": "off",
        "PEBBLE_TOOL_PORT": str(tool_port),
        "PEBBLE_QODER_MODEL": args.agent_model,
        "PEBBLE_LIGHT_MODEL": "",
        "PEBBLE_LIGHT_MODEL_PROVIDER": "",
        "PEBBLE_LIGHT_MODEL_API_KEY": "",
        "PEBBLE_LIGHT_MODEL_BASE_URL": "",
        "PEBBLE_ICLOUD_ACCOUNT": "virtual@example.test",
        "PEBBLE_ICLOUD_PASSWORD_PATH": str(directory / "absent"),
        "PEBBLE_ICLOUD_CALENDAR_URL": "https://virtual.icloud.com/primary/",
        "PEBBLE_GMAIL_CREDENTIALS_PATH": str(directory / "absent"),
        "PEBBLE_GMAIL_TOKEN_PATH": str(directory / "absent"),
    }
    api = PebbleClient(f"http://127.0.0.1:{service_port}")
    runner = Runner(api, directory, args.agent_model)
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen(
            [
                "uv",
                "run",
                "--project",
                "server",
                "python",
                "-m",
                "uvicorn",
                "scenario_eval.service:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(service_port),
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("隔离服务启动失败，见server.log")
                try:
                    api.health()
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise TimeoutError("隔离服务未就绪")
            if world.get("entry") == "incoming":
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    tasks = api._request("GET", "/api/tasks")
                    if tasks:
                        task = tasks[0]["task_id"]
                        runner.tasks.append(task)
                        latest = api.get_task(task).get("latest_run")
                        if latest:
                            runner.wait_turn(task, latest)
                            break
                    time.sleep(1)
                else:
                    raise TimeoutError("新邮件没有触发任务")
            if world.get("initial_message"):
                runner.execute(
                    {
                        "action": "say" if runner.tasks else "start_task",
                        "task_id": runner.tasks[0] if runner.tasks else None,
                        "message": world["initial_message"],
                        "interaction_type": "planned",
                        "reason": "场景初始交办",
                    }
                )
            fixed_user = (scenario / "user.md").read_text()
            for index in range(args.max_actions):
                payload = {"user_setting": fixed_user, "conversation": runner.context()}
                decision = call(USER, payload, args.user_model, directory / "user" / str(index))
                print(
                    f"用户动作 {index + 1}: {decision.get('action')} "
                    f"{decision.get('message', decision.get('reason', ''))}",
                    flush=True,
                )
                try:
                    if not runner.execute(decision):
                        break
                except (ValueError, KeyError, ApiError) as error:
                    # 明确拒绝可作为用户可见操作错误继续；5xx结果不确定时不重发。
                    if isinstance(error, ApiError) and error.status >= 500:
                        raise
                    runner.record("action_error", error=str(error))
            else:
                runner.record("limit_reached", max_actions=args.max_actions)
        except Exception as error:  # noqa: BLE001 保留失败事实，仍尝试一次评审
            runner.record("infrastructure_error", error=f"{type(error).__name__}: {error}")
            print(f"运行异常: {type(error).__name__}: {error}", flush=True)
        finally:
            try:
                evidence = runner.evidence(initial)
            except Exception as error:  # noqa: BLE001 取证失败不得推定通过
                evidence = {
                    "initial": initial,
                    "events": runner.records,
                    "collection_error": str(error),
                    "database": database(directory / "data" / "pebble.db"),
                }
            save(directory / "evidence.json", evidence)
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    payload = {
        "scenario": {
            "user": (scenario / "user.md").read_text(),
            "expected": (scenario / "expected.md").read_text(),
        },
        "evidence": evidence,
    }
    try:
        review = call(REVIEW, payload, args.review_model, directory / "review")
        for key in ("result", "process"):
            if review.get(key) not in {"pass", "fail", "unclear"}:
                raise ValueError("评审格式不合法")
    except Exception as error:  # noqa: BLE001 不重评，不把模型故障当成产品失败
        review = {
            "result": "unclear",
            "process": "unclear",
            "outcome": "待复核",
            "reason": f"评审调用或格式失败: {type(error).__name__}: {error}",
        }
    save(directory / "report.json", review)
    (directory / "report.md").write_text(
        "# 多轮场景评测\n\n真实Qoder模型；虚拟邮箱/日历；受控服务器跨日。\n\n"
        + f"结果：{review.get('outcome', '未分类')}\n\n"
        + f"结果正确：{review['result']}；过程合理：{review['process']}\n\n"
        + review.get("reason", "")
        + "\n\n```json\n"
        + json.dumps(review, ensure_ascii=False, indent=2)
        + "\n```\n"
    )
    print(json.dumps(review, ensure_ascii=False, indent=2), flush=True)
    print(f"报告: {directory / 'report.md'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", type=Path, default=Path(__file__).parent / "scenarios" / "colleague_followup"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--agent-model", default="qwen3.8-max")
    parser.add_argument("--user-model", default="deepseek-flash")
    parser.add_argument("--review-model", default="deepseek-flash")
    parser.add_argument("--max-actions", type=int, default=20)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
