"""手动走完整邮件流程用的可启动后端：真实服务 + 模拟邮箱 + 会话式 Agent 替身。

服务端的任务、草稿版本、确认执行、SSE 与持久化都是真的；邮件内容、摘要建议、
草稿改写和 Gmail 发送是替身。收件箱目录默认 `<数据目录>/inbox`，用
`python -m tests.mail_inbox deliver <名字>` 投递；仅用于人工验收，不是生产启动方式。
"""

from fastapi import FastAPI

from server.config import get_settings
from server.main import create_app
from tests.support.gmail_double import send, validate
from tests.support.mailbox import MockMailbox, inbox_dir
from tests.support.mock_agent import MockAgent


def build_app() -> FastAPI:
    """按当前配置装配一套替身后端；邮箱与 Agent 可从 app.state 取到。"""
    mailbox = MockMailbox(inbox_dir())
    gateway = MockAgent(mailbox, state_dir=get_settings().data_dir)
    application = create_app(
        gateway=gateway,
        validate_reply_draft=validate,
        send_reply=send,
        mail_source=mailbox,
    )
    gateway.bind(application)
    return application


app = build_app()
