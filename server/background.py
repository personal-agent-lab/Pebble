"""Gmail 增量检测：先交给任务入口，再推进游标；回复发送不在此执行。"""

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

from googleapiclient.errors import HttpError

from server.config import get_settings
from server.tools.gmail.client import GoogleApiGmailClient

logger = logging.getLogger(__name__)


class GmailSource:
    def __init__(self, client: GoogleApiGmailClient, path: Path | None = None, interval=10):
        self.client = client
        self.path = path or get_settings().data_dir / "gmail_sync.json"
        self.interval = interval
        self.error = None
        self.worker = None
        self.stopping = asyncio.Event()

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(self.path)
        self.state = state

    async def start(self, agent):
        self.agent = agent
        profile = await asyncio.to_thread(
            lambda: self.client.get_service().users().getProfile(userId="me").execute()
        )
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state["email"] != profile["emailAddress"]:
                raise RuntimeError("Gmail 同步账号与已有游标不一致")
        else:
            # 首次启动从当前邮箱位置监听，不把已有历史邮件当成新邮件。
            self._save({"email": profile["emailAddress"], "history_id": profile["historyId"]})
        self.worker = asyncio.create_task(self._run())

    async def poll(self):
        token = None
        while True:

            def fetch(page_token=token):
                return (
                    self.client.get_service()
                    .users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=self.state["history_id"],
                        historyTypes=["messageAdded"],
                        pageToken=page_token,
                    )
                    .execute()
                )

            page = await asyncio.to_thread(fetch)
            for change in page.get("history", []):
                for added in change.get("messagesAdded", []):
                    message = added["message"]
                    if "INBOX" in message.get("labelIds", []):
                        self.agent.accept_new_mail(message["id"], message["threadId"])
            token = page.get("nextPageToken")
            if not token:
                self._save({**self.state, "history_id": page["historyId"]})
                self.error = None
                return

    async def _run(self):
        while not self.stopping.is_set():
            try:
                await self.poll()
            except HttpError as error:
                self.error = f"Gmail 检测失败（HTTP {error.resp.status}）"
                if error.resp.status == 404:
                    self.error = "Gmail 同步游标已失效，需要核对邮箱并重新建立同步位置"
                    logger.error(self.error)
                    return
                logger.error(self.error)
            except Exception as error:
                self.error = f"Gmail 检测失败（{type(error).__name__}），游标未推进"
                logger.error(self.error)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=self.interval)

    async def stop(self):
        self.stopping.set()
        if self.worker is not None:
            await self.worker
