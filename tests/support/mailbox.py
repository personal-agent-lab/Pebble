"""模拟邮箱：以目录里的 json 文件代替 Gmail 收件箱，仅供手动验收与测试使用。

按固定间隔扫描目录，把每封邮件的标识交给 `GatewayRuntime.accept_new_mail`；
去重仍由服务端的持久关联保证，所以同一封邮件反复扫描不会重复建任务。
真实 Gmail 检测由 B 实现同一套 `MailSource` 启停接口，本模块不进入默认装配。
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from server.config import get_settings
from server.gateway.runtime import GatewayRuntime

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0

REQUIRED_FIELDS = ("message_id", "thread_id", "from", "subject", "body")


def inbox_dir() -> Path:
    """收件箱目录：默认 `<数据目录>/inbox`，可用 PEBBLE_TEST_INBOX_DIR 指定。"""
    configured = os.environ.get("PEBBLE_TEST_INBOX_DIR")
    return Path(configured) if configured else get_settings().data_dir / "inbox"


def load_mail(path: Path) -> dict:
    """读取一封邮件文件；缺必需字段按坏邮件处理，不猜默认值。"""
    mail = json.loads(path.read_text(encoding="utf-8"))
    missing = [field for field in REQUIRED_FIELDS if not mail.get(field)]
    if missing:
        raise ValueError(f"邮件文件缺字段 {missing}：{path}")
    return mail


class MockMailbox:
    """目录邮箱：既是新邮件来源，也是替身 Agent 读取邮件内容的地方。"""

    def __init__(self, directory: Path, *, interval: float = POLL_SECONDS):
        self.directory = Path(directory)
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._known: dict[str, str] = {}
        self._broken: set[str] = set()

    # ---------- 邮件内容 ----------

    def list_mails(self) -> list[dict]:
        if not self.directory.is_dir():
            return []
        mails = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                mails.append(load_mail(path))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                if str(path) not in self._broken:
                    self._broken.add(str(path))
                    logger.warning("跳过无法读取的邮件文件：%s", error)
        return mails

    def read(self, message_id: str) -> dict | None:
        """按邮件 ID 读取内容；替身 Agent 用它代替 Gmail 读取接口。"""
        for mail in self.list_mails():
            if mail["message_id"] == message_id:
                return mail
        return None

    # ---------- 检测来源 ----------

    def deliver_once(self, agent: GatewayRuntime) -> list[dict]:
        """扫描一轮；返回本轮识别为新邮件的任务，重复邮件由服务端去重。"""
        started = []
        for mail in self.list_mails():
            message_id = mail["message_id"]
            task = agent.accept_new_mail(message_id, mail["thread_id"])
            if self._known.get(message_id) == task["task_id"]:
                continue
            self._known[message_id] = task["task_id"]
            logger.info("新邮件 %s 交给任务 %s", message_id, task["task_id"])
            started.append(task)
        return started

    async def start(self, agent: GatewayRuntime) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._poll(agent))

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _poll(self, agent: GatewayRuntime) -> None:
        while True:
            try:
                self.deliver_once(agent)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 检测失败不该停掉整个邮箱：记下来，下一轮继续。
                logger.exception("邮件检测失败，下一轮重试")
            await asyncio.sleep(self.interval)
