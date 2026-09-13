"""tests/test_prepare_reply.py: 测试 prepare_reply 工具、业务校验门禁与草稿去重。"""

from server.tools.gmail.client import MockGmailClient
from server.tools.gmail.protocol import InMemoryDraftStorage
from server.tools.gmail.tools import prepare_reply
from server.tools.registry import SideEffect, default_registry


def test_prepare_reply_registered_as_local_write() -> None:
    tool_def = default_registry.get_tool("gmail_prepare_reply")
    assert tool_def is not None
    assert tool_def.side_effect == SideEffect.LOCAL_WRITE
    # LOCAL_WRITE 应该对模型可见（允许生成草稿供审阅）
    exposed_names = [t.name for t in default_registry.get_model_exposed_tools()]
    assert "gmail_prepare_reply" in exposed_names


def test_prepare_reply_validation_failure_blocks_saving() -> None:
    storage = InMemoryDraftStorage()

    # 传入非法邮箱与空主题
    res = prepare_reply(
        task_id="task_001",
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["invalid-email-address"],
        subject="  ",
        body="这是测试正文",
        storage=storage,
        client=MockGmailClient(),
    )

    assert res["success"] is False
    assert "草稿校验失败" in res["error"]
    err_fields = [e["field"] for e in res["validation_errors"]]
    assert "to" in err_fields
    assert "subject" in err_fields

    # 核心保证：校验失败绝不持久化任何数据
    assert len(storage.drafts) == 0


def test_prepare_reply_success_creates_draft_pending_review() -> None:
    storage = InMemoryDraftStorage()

    res = prepare_reply(
        task_id="task_001",
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["Alice <alice@example.com>"],
        subject="Re: 项目进展评审与架构讨论邀请",
        body="已收到会议邀请，我将准时出席。",
        storage=storage,
        client=MockGmailClient(),
    )

    assert res["success"] is True
    op_id = res["operation_id"]
    assert op_id.startswith("op_")
    assert res["version"] == 1
    assert res["status"] == "pending"
    assert "已成功保存为待审阅状态" in res["message"]

    # 验证底层存储已记录
    draft = storage.get_draft(op_id)
    assert draft is not None
    assert draft["task_id"] == "task_001"
    assert draft["source_message_id"] == "msg_invite_001"
    assert draft["to"] == ["Alice <alice@example.com>"]


def test_prepare_reply_deduplication_reuses_existing_operation() -> None:
    storage = InMemoryDraftStorage()

    # 第 1 次准备回复
    res1 = prepare_reply(
        task_id="task_001",
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["alice@example.com"],
        subject="Re: 邀请",
        body="第一次准备回复。",
        storage=storage,
        client=MockGmailClient(),
    )
    assert res1["success"] is True
    op1 = res1["operation_id"]

    # 第 2 次针对同一封原邮件重复准备（例如 Agent 重复触发或未要求重写）
    res2 = prepare_reply(
        task_id="task_002",
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["alice@example.com"],
        subject="Re: 邀请",
        body="第二次准备回复。",
        storage=storage,
        client=MockGmailClient(),
    )
    assert res2["success"] is True
    op2 = res2["operation_id"]

    # 核心去重断言：复用已有 operation_id，底层存储依然只有一份草稿
    assert op1 == op2
    assert len(storage.drafts) == 1


def test_prepare_reply_rejects_recipient_other_than_source_sender() -> None:
    storage = InMemoryDraftStorage()
    res = prepare_reply(
        task_id="task_001",
        source_message_id="msg_invite_001",
        thread_id="thread_invite_001",
        to=["user@example.com"],
        subject="Re: 邀请",
        body="我会参加。",
        storage=storage,
        client=MockGmailClient(),
    )

    assert res["success"] is False
    assert res["validation_errors"][0]["field"] == "to"
    assert not storage.drafts


def test_prepare_reply_prefers_reply_to_over_from() -> None:
    storage = InMemoryDraftStorage()
    client = MockGmailClient()
    source = client.messages["msg_invite_001"]
    client.messages[source.id] = source.__class__(
        **{**source.__dict__, "reply_to_addrs": ["events@example.net"]}
    )

    rejected = prepare_reply(
        task_id="task_001",
        source_message_id=source.id,
        thread_id=source.thread_id,
        to=["alice@example.com"],
        subject="Re: 邀请",
        body="我会参加。",
        storage=storage,
        client=client,
    )
    accepted = prepare_reply(
        task_id="task_001",
        source_message_id=source.id,
        thread_id=source.thread_id,
        to=["events@example.net"],
        subject="Re: 邀请",
        body="我会参加。",
        storage=storage,
        client=client,
    )

    assert rejected["success"] is False
    assert accepted["success"] is True
