"""七步邮件演示的 B 替身：固定邮件、建议和草稿内容，不调用真实服务。"""

from tests.support.backend_fixture import app as app
from tests.support.backend_fixture import gateway

MAIL = {
    "from": "organizer@example.com",
    "subject": "周五项目交流会邀请",
    "body": "你好，邀请你本周五下午 3 点参加线上项目交流会，请回复是否参加。",
}
DRAFTS = {
    "帮我写一封回信": "您好，\n\n感谢邀请，我确认参加本周五下午 3 点的项目交流会。\n\n谢谢！",
    "请再询问一下会议链接": (
        "您好，\n\n感谢邀请，我确认参加本周五下午 3 点的项目交流会。"
        "请问方便提供会议链接吗？\n\n谢谢！"
    ),
}


async def analyze(**fields):
    yield {"type": "session", "sdk_session_id": fields["sdk_session_id"] or "demo-session"}
    yield {
        "type": "text",
        "text": (
            "摘要：对方邀请你周五下午 3 点参加线上项目交流会。建议：回复确认参加，并询问会议链接。"
        ),
    }
    yield {"type": "done"}


async def prepare(*, task_id, sdk_session_id, message):
    yield {"type": "session", "sdk_session_id": sdk_session_id}
    operations = app.state.tasks.list_task_operations(task_id)
    fields = {"to": [MAIL["from"]], "subject": "回复：" + MAIL["subject"], "body": DRAFTS[message]}
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
    yield {"type": "text", "text": "回信草稿已保存，请审核。"}
    yield {"type": "done"}


gateway.handle("new_mail", analyze)
gateway.handle("message", prepare)
