"""对话框里发出的消息让任务里待确认的草稿失效；卡片上的修改要求只改那张，不取消。"""

import asyncio
import json

import pytest

from server.db import init_db
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from tests.support.agent_double import FakeAgentGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def world(settings):
    init_db()
    gateway = FakeAgentGateway()

    def handler(turn):
        async def events():
            yield {"type": "session", "sdk_session_id": turn.sdk_session_id or "s"}
            yield {"type": "text", "text": "好的"}
            yield {"type": "done"}

        return events()

    gateway.handle("message", handler)
    service = GatewayRuntime(gateway)
    try:
        yield service, SessionStore(), MailDraftStore()
    finally:
        async with asyncio.timeout(5):
            while service._active or service._titles:
                await asyncio.gather(*list(service._active.values()), *list(service._titles))
        await service.close()


async def test_chat_message_cancels_pending_drafts_at_once(world):
    service, tasks, drafts = world
    task_id = tasks.create_task("写一封近况邮件")["task_id"]
    first = drafts.save_email_draft(task_id, ["a@example.com"], "近况", "正文")
    second = drafts.save_email_draft(task_id, ["b@example.com"], "近况", "正文")
    other_task = tasks.create_task("别的任务")["task_id"]
    elsewhere = drafts.save_email_draft(other_task, ["c@example.com"], "别处", "正文")

    # 定向某张卡片的修改要求不取消任何草稿。
    target = {"kind": "mail_draft", "operation_id": first["operation_id"]}
    service.submit_message(task_id, "改短一点", target=json.loads(json.dumps(target)))
    assert drafts.get_draft(first["operation_id"])["status"] == "pending"

    # 对话框里的消息一发出，本任务里待确认的草稿立即取消，别的任务不受影响。
    service.submit_message(task_id, "我在南京")
    assert drafts.get_draft(first["operation_id"])["status"] == "cancelled"
    assert drafts.get_draft(second["operation_id"])["status"] == "cancelled"
    assert drafts.get_draft(elsewhere["operation_id"])["status"] == "pending"
