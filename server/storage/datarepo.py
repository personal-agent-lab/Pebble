"""实例数据目录的共享协调点：进程内串行锁与版本仓库忽略规则。

`memory/`、`kb/`、`skills/` 同属数据目录里**同一个**本地 Git 仓库。不同域的写入必须
串行，否则并发提交会争用 `git index.lock`；忽略规则也必须一致，否则各域会写出不同的
`.gitignore`。这里只放这两样真正需要共享的东西，各域自己的 Git/原子写小函数留在各自模块。
"""

from __future__ import annotations

import threading
from pathlib import Path

# 忽略一切，再逐项放行内容目录：SQLite、凭证、SDK 会话与日志都不进版本管理。
GITIGNORE = """*
!.gitignore
!memory/
!memory/**
!kb/
!kb/**
!skills/
!skills/**
!skill_drafts/
!skill_drafts/**
"""

_locks_guard = threading.Lock()
_locks: dict[Path, threading.RLock] = {}


def lock_for(path: Path) -> threading.RLock:
    """按解析后的数据目录路径返回进程内共享的可重入锁。

    指向同一数据目录的所有存储共用一把锁，使跨域的磁盘写入与 Git 提交相互串行。
    """
    resolved = path.resolve()
    with _locks_guard:
        return _locks.setdefault(resolved, threading.RLock())
