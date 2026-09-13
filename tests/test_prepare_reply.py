"""tests/test_prepare_reply.py: 测试 prepare_reply 工具、业务校验门禁与草稿去重。

草稿存储用真实 SQLite：工具与 HTTP 共用同一实现，去重和版本由数据库唯一约束保证。
"""

import pytest

from server.db import init_db
from server.errors import NotFoundError
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore
from server.tools.gmail.tools import prepare_reply
from server.tools.gmail.validator import validate_reply_draft
from server.tools.registry import SideEffect, default_registry


@pytest.fixture
def drafts(settings) -> ReplyDraftStore:
    init_db()
    return ReplyDraftStore(validate_reply_draft)


@pytest.fixture
def task_id(settings) -> str:
    init_db()
    return SessionStore().create_task("处理新收到的邮件")["task_id"]


def test_prepare_reply_registered_as_local_write() -> None:
    tool_def = default_registry.get_tool("gmail_prepare_reply")
    assert tool_def is not None
    assert tool_def.side_effect == SideEffect.LOCAL_WRITE
    # LOCAL_WRITE 应该对模型可见（允许生成草稿供审阅）
    exposed_names = [t.name for t in default_registry.get_model_exposed_tools()]
    assert "gmail_prepare_reply" in exposed_names


def test_prepare_reply_validation_failure_blocks_saving(drafts, task_id) -> None:
    # 传入非法邮箱与空主题
    res = prepare_reply(
        task_id=task_id,
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["invalid-email-address"],
        subject="  ",
        body="这是测试正文",
        drafts=drafts,
    )

    assert res["success"] is False
    assert "草稿校验失败" in res["error"]
    err_fields = [e["field"] for e in res["validation_errors"]]
    assert "to" in err_fields
    assert "subject" in err_fields

    # 核心保证：校验失败绝不持久化任何数据
    assert SessionStore().list_task_operations(task_id) == []


def test_prepare_reply_success_creates_draft_pending_review(drafts, task_id) -> None:
    res = prepare_reply(
        task_id=task_id,
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["Alice <alice@example.com>"],
        subject="Re: 项目进展评审与架构讨论邀请",
        body="已收到会议邀请，我将准时出席。",
        drafts=drafts,
    )

    assert res["success"] is True
    op_id = res["operation_id"]
    assert res["version"] == 1
    assert res["status"] == "pending"
    assert "已成功保存为待审阅状态" in res["message"]

    # 验证底层存储已记录，并关联到当前任务
    draft = drafts.get_reply_draft(op_id)
    assert draft["source_message_id"] == "msg_invite_001"
    assert draft["thread_id"] == "thread_invite_001"
    assert draft["to"] == ["Alice <alice@example.com>"]
    assert [op["operation_id"] for op in SessionStore().list_task_operations(task_id)] == [op_id]


def test_prepare_reply_deduplication_reuses_existing_operation(drafts, settings) -> None:
    tasks = SessionStore()
    init_db()
    first_task = tasks.create_task("第一封邮件任务")["task_id"]
    second_task = tasks.create_task("同一封邮件的另一个任务")["task_id"]

    res1 = prepare_reply(
        task_id=first_task,
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["alice@example.com"],
        subject="Re: 邀请",
        body="第一次准备回复。",
        drafts=drafts,
    )
    assert res1["success"] is True

    # 第 2 次针对同一封原邮件重复准备（例如 Agent 重复触发或未要求重写）
    res2 = prepare_reply(
        task_id=second_task,
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["alice@example.com"],
        subject="Re: 邀请",
        body="第二次准备回复。",
        drafts=drafts,
    )
    assert res2["success"] is True

    # 核心去重断言：复用已有 operation_id，内容仍是第 1 版
    assert res1["operation_id"] == res2["operation_id"]
    assert res2["version"] == 1
    assert drafts.get_reply_draft(res1["operation_id"])["body"] == "第一次准备回复。"
    # 复用把已有操作关联到本次任务，不新建操作
    assert [op["operation_id"] for op in tasks.list_task_operations(second_task)] == [
        res1["operation_id"]
    ]


def test_prepare_reply_rejects_unknown_task(drafts, settings) -> None:
    init_db()
    with pytest.raises(NotFoundError):
        prepare_reply(
            task_id="not-a-task",
            source_message_id="msg_invite_001",
            thread_id="thread_invite_001",
            to=["alice@example.com"],
            subject="Re: 邀请",
            body="正文",
            drafts=drafts,
        )
