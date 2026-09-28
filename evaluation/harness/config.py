"""评测运行配置：路径、端口与超时。"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

# 评测产物面向本地阅读，时间戳统一按 UTC+8（北京时间）呈现，不用 UTC。
EVAL_TZ = timezone(timedelta(hours=8), "UTC+8")


def repo_root() -> Path:
    """代码仓库根目录（本包位于 <root>/evaluation/harness/）。"""
    return Path(__file__).resolve().parents[2]


def seeds_root() -> Path:
    """评测语料根目录（<root>/evaluation/seeds）：`kb/` 与两份记忆的原文。"""
    return Path(__file__).resolve().parents[1] / "seeds"


def free_port() -> int:
    """让操作系统分配一个空闲回环端口，避免评测实例与开发实例冲突。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class EvalSettings:
    """一次评测运行的参数。模型与凭证沿用实例环境（env / .env），不在此重复配置。"""

    model: str = ""
    boot_timeout: float = 120.0
    turn_timeout: float = 420.0
    settle_timeout: float = 120.0
    poll_interval: float = 2.0
    results_root: Path = field(default_factory=lambda: repo_root() / ".eval-results")

    @property
    def effective_model(self) -> str:
        """评测用主模型：显式参数 > PEBBLE_EVAL_MODEL > 实例默认。"""
        return (
            self.model
            or os.environ.get("PEBBLE_EVAL_MODEL", "")
            or os.environ.get("PEBBLE_QODER_MODEL", "auto")
        )
