"""隔离实例：临时数据目录 + 独立端口启动 Pebble 服务，结束后保留数据目录作证据。

不配置 Gmail / iCloud 凭证即得到 S 层运行前提：两域 `unconfigured`，其余功能可用。
模型凭证（QODERCN_PERSONAL_ACCESS_TOKEN 等）继承评测进程环境与仓库 `.env`。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .client import PebbleClient
from .config import EvalSettings, free_port, repo_root


class ServerBootError(RuntimeError):
    """服务未能在超时内通过健康检查。"""


def _neutralize_external_services(data_dir: Path) -> dict[str, str]:
    """指向不存在的凭证文件，使隔离实例两域 unconfigured。

    仓库 `.env` 里的真实 Gmail / iCloud 配置会被子进程读到；不中和的话，
    S 层评测就会挂在真实账号上。子进程显式 env 优先于 `.env`，足以屏蔽。
    M / C 层接入测试账号时，在数据目录放置测试凭证后改写这几项即可。
    """
    absent = data_dir / "absent"
    return {
        "PEBBLE_GMAIL_CREDENTIALS_PATH": str(absent / "gmail-credentials.json"),
        "PEBBLE_GMAIL_TOKEN_PATH": str(absent / "gmail-token.json"),
        "PEBBLE_ICLOUD_ACCOUNT": "",
        "PEBBLE_ICLOUD_PASSWORD_PATH": str(absent / "icloud-password.txt"),
        "PEBBLE_ICLOUD_CALENDAR_URL": "",
    }


class InstanceServer:
    """一个用完即弃的 Pebble 实例。同一场景的每次重复使用独立实例。

    expect_unconfigured：启动后两域必须报 `unconfigured`（S 层前提）；M / C 层
    装好测试账号凭证后置 False。
    """

    def __init__(
        self, settings: EvalSettings, run_dir: Path, *, expect_unconfigured: bool = True
    ) -> None:
        self.settings = settings
        self.run_dir = run_dir
        self.data_dir = run_dir / "data"
        self.expect_unconfigured = expect_unconfigured
        self.port = free_port()
        self.tool_port = free_port()
        self._process: subprocess.Popen[bytes] | None = None
        self._client: PebbleClient | None = None

    @property
    def client(self) -> PebbleClient:
        if self._client is None:
            raise ServerBootError("实例尚未启动")
        return self._client

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "PEBBLE_DATA_DIR": str(self.data_dir),
            "PEBBLE_PORT": str(self.port),
            "PEBBLE_TOOL_PORT": str(self.tool_port),
            "PEBBLE_AUTH": "off",
            **_neutralize_external_services(self.data_dir),
        }
        log_path = self.run_dir / "server.log"
        command = [
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
        ]
        with log_path.open("ab") as log_file:
            self._process = subprocess.Popen(
                command,
                cwd=repo_root(),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        self._client = PebbleClient(f"http://127.0.0.1:{self.port}")
        self._wait_healthy(log_path)

    def _wait_healthy(self, log_path: Path) -> None:
        deadline = time.monotonic() + self.settings.boot_timeout
        last_error = "未知错误"
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                break
            try:
                health = self.client.health()
                if health.get("status") in {"ok", "degraded"}:
                    self._guard_unconfigured(health)
                    (self.run_dir / "health.json").write_text(
                        json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    return
                last_error = f"health status={health.get('status')!r}"
            except Exception as exc:  # noqa: BLE001 启动期任何失败都重试到超时
                last_error = repr(exc)
            time.sleep(1.0)
        tail = self._log_tail(log_path)
        raise ServerBootError(f"服务未就绪（{last_error}）。\nserver.log 末尾：\n{tail}")

    def _guard_unconfigured(self, health: dict[str, object]) -> None:
        """两域必须是 unconfigured：中和失效（真实账号被挂上）比测不了更糟，立即熔断。"""
        if not self.expect_unconfigured:
            return
        services = health.get("services") or {}
        configured = {
            name: state
            for name, state in services.items()
            if isinstance(state, dict) and state.get("status") != "unconfigured"
        }
        if configured:
            raise ServerBootError(
                f"隔离实例外部服务未被中和（{configured}）；评测实例严禁挂真实账号，"
                "请检查 PEBBLE_GMAIL_* / PEBBLE_ICLOUD_* 环境覆盖。"
            )

    @staticmethod
    def _log_tail(log_path: Path, lines: int = 40) -> str:
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "（无法读取日志）"
        return "\n".join(content[-lines:])

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)
        self._process = None

    def __enter__(self) -> InstanceServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
