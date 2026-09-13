"""模拟邮箱：以目录里的 json 文件代替 Gmail 收件箱，仅供手动验收与测试使用。

按固定间隔扫描目录，把每封邮件的标识交给 `GatewayRuntime.accept_new_mail`；
去重仍由服务端的持久关联保证，所以同一封邮件反复扫描不会重复建任务。
发现的邮件同步进 `self.client`（MockGmailClient），替身 Agent 与真实工具走同一套
`BaseGmailClient` 读取接口；json 里的摘要、建议等字段只是替身脚本素材，不是邮件内容。
真实 Gmail 检测由 B 实现同一套 `MailSource` 启停接口，本模块不进入默认装配。
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from server.config import get_settings
from server.gateway.runtime import GatewayRuntime
from server.tools.gmail.client import GmailMessage
from tests.support.gmail_double import MockGmailClient

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


def as_message(mail: dict) -> GmailMessage:
    """邮件文件转成 GmailMessage，与真实客户端给出的数据结构一致。"""
    return GmailMessage(
        id=mail["message_id"],
        thread_id=mail["thread_id"],
        rfc_message_id=f"<{mail['message_id']}@mock.local>",
        from_addr=mail["from"],
        to_addrs=list(mail.get("to", [])),
        subject=mail["subject"],
        snippet=mail["body"][:50],
        body_text=mail["body"],
        date=mail.get("received_at", ""),
        labels=["INBOX"],
    )


class MockMailbox:
    """目录邮箱：新邮件来源；发现的邮件同步进 `client`，供替身 Agent 按真实接口读取。"""

    def __init__(self, directory: Path, *, interval: float = POLL_SECONDS):
        self.directory = Path(directory)
        self.interval = interval
        self.client = MockGmailClient()
        # 替身客户端自带一封演示种子邮件；目录邮箱的内容以邮件文件为准，不混入种子。
        self.client.messages.clear()
        self.client.threads.clear()
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

    def _sync(self, mail: dict) -> None:
        """新发现的邮件登记进替身客户端；同线程的邮件追加到同一线程。"""
        message = as_message(mail)
        if message.id in self.client.messages:
            return
        self.client.messages[message.id] = message
        self.client.threads.setdefault(message.thread_id, []).append(message.id)

    def hints(self, message_id: str) -> dict:
        """邮件文件里的替身脚本素材（摘要、建议、起草正文）；没有对应文件时为空。"""
        for mail in self.list_mails():
            if mail["message_id"] == message_id:
                return mail
        return {}

    # ---------- 检测来源 ----------

    def deliver_once(self, agent: GatewayRuntime) -> list[dict]:
        """扫描一轮；返回本轮识别为新邮件的任务，重复邮件由服务端去重。"""
        started = []
        for mail in self.list_mails():
            self._sync(mail)
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
