"""仅测试用的可启动后端：真实服务 + 有脚本的 Agent/Gmail 替身。"""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from server.main import create_app
from tests.support.agent_double import FakeAgentGateway


def validate(**fields):
    if not fields["body"]:
        return {"valid": False, "errors": [{"field": "body", "message": "正文不能为空"}]}
    return {"valid": True, "errors": []}


def send(**fields):
    with (Path(os.environ["PEBBLE_DATA_DIR"]) / "sent.jsonl").open("a") as output:
        output.write(json.dumps(fields, ensure_ascii=False) + "\n")
    status = os.environ.get("PEBBLE_TEST_SEND_STATUS", "sent")
    if status == "sent":
        return {"status": status, "message_id": "test-message"}
    return {"status": status, "reason": "测试替身模拟结果：" + status}


gateway = FakeAgentGateway()
app = create_app(gateway=gateway, validate_reply_draft=validate, send_reply=send)


async def reply(*, task_id, sdk_session_id, message):
    yield {"type": "session", "sdk_session_id": sdk_session_id or "test-session"}
    operations = app.state.tasks.list_task_operations(task_id)
    fields = {"to": ["a@example.com", "b@example.com"], "subject": " 回复：邀请 ", "body": message}
    if operations:
        operation = operations[0]
        saved = app.state.drafts.update_reply_draft(
            operation["operation_id"], operation["version"], **fields
        )
    else:
        saved = app.state.drafts.save_reply_draft(
            task_id, "fixture-mail", "fixture-thread", **fields
        )
    yield {
        "type": "draft_saved",
        "operation_id": saved["operation_id"],
        "version": saved["version"],
    }
    yield {"type": "text", "text": "草稿已保存，请审核。"}
    yield {"type": "done"}


gateway.handle("message", reply)

# 模拟 B 检测到一封邮件；启动时同一邮件重复投递用于验证持久去重。
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        application.state.agent.accept_new_mail("fixture-mail", "fixture-thread")
        application.state.agent.accept_new_mail("fixture-mail", "fixture-thread")
        yield


app.router.lifespan_context = lifespan
