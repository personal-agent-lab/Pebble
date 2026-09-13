"""Gmail 工具集：向 Agent 暴露的查询工具与草稿准备工具。

严格遵守安全红线：
- 查询工具标记为 READONLY，模型可自主调用；
- 外部写操作（发送邮件）不在此注册，模型不可见。

客户端与存储是仅关键字参数，参数名与 `server/agent/toolset.py` 的 `ToolDeps` 字段一致：
装配时按名字绑定，不进入模型 schema，也不在模块内查找全局单例；未装配的依赖不会出现在
工具集里，不退回模拟实现。
"""

from __future__ import annotations

from typing import Any

from server.errors import NotFoundError
from server.sessions.service import SessionStore
from server.tools.gmail.client import BaseGmailClient
from server.tools.gmail.service import ReplyDraftStore
from server.tools.registry import SideEffect, tool


@tool(
    name="gmail_query_emails",
    description=(
        "根据关键词、主题或发件人搜索 Gmail 邮件列表。"
        "返回匹配邮件的基础信息（ID、ThreadId、发件人、主题、摘要、日期）。"
    ),
    side_effect=SideEffect.READONLY,
)
def query_emails(
    query: str,
    max_results: int = 10,
    *,
    gmail: BaseGmailClient,
) -> list[dict[str, Any]]:
    """搜索邮件列表并返回匹配的摘要信息。"""
    search_results = gmail.search_messages(query, max_results=max_results)

    email_summaries: list[dict[str, Any]] = []
    for item in search_results:
        msg_id = item["id"]
        try:
            msg = gmail.get_message(msg_id)
            email_summaries.append(
                {
                    "id": msg.id,
                    "thread_id": msg.thread_id,
                    "from": msg.from_addr,
                    "to": msg.to_addrs,
                    "subject": msg.subject,
                    "snippet": msg.snippet,
                    "date": msg.date,
                }
            )
        except Exception:
            # 容错：个别邮件获取失败时至少返回 id 与 thread_id
            email_summaries.append(
                {
                    "id": msg_id,
                    "thread_id": item.get("threadId", ""),
                    "snippet": "未能获取详细信息",
                }
            )

    return email_summaries


def format_thread_transcript(messages: list[Any]) -> str:
    """将按时间排序的往来邮件整理为清晰易懂的对话文本，注入模型上下文。"""
    if not messages:
        return "该线程内暂无历史邮件。"

    lines = [f"=== 邮件往来历史（共 {len(messages)} 封，按时间先后正序排列）==="]
    for idx, m in enumerate(messages, start=1):
        lines.append(f"\n【第 {idx} 封往来 | 时间: {m.date or '未知'}】")
        lines.append(f"• 发件人: {m.from_addr}")
        lines.append(f"• 收件人: {', '.join(m.to_addrs)}")
        if m.cc_addrs:
            lines.append(f"• 抄送: {', '.join(m.cc_addrs)}")
        lines.append(f"• 主题: {m.subject}")
        lines.append(f"• Message-ID: {m.rfc_message_id}")
        lines.append(f"• 内部ID: {m.id}")
        lines.append("• 正文内容:")
        body = m.body_text.strip() if m.body_text else m.snippet.strip()
        lines.append(body)
        lines.append("-" * 40)
    return "\n".join(lines)


@tool(
    name="gmail_get_thread",
    description=(
        "获取指定邮件线程（Thread）内的所有历史往来邮件，按时间先后顺序排列。"
        "返回结构化列表与排版清晰的时间线文本（transcript），注入模型上下文以补充背景。"
    ),
    side_effect=SideEffect.READONLY,
)
def get_email_thread(
    thread_id: str,
    *,
    gmail: BaseGmailClient,
) -> dict[str, Any]:
    """获取线程内全部往来邮件详情，生成时间线对话记录注入模型上下文。"""
    messages = gmail.get_thread(thread_id)

    structured_messages = [
        {
            "sequence": idx + 1,
            "id": m.id,
            "thread_id": m.thread_id,
            "rfc_message_id": m.rfc_message_id,
            "from": m.from_addr,
            "to": m.to_addrs,
            "cc": m.cc_addrs,
            "subject": m.subject,
            "body": m.body_text,
            "date": m.date,
        }
        for idx, m in enumerate(messages)
    ]

    return {
        "thread_id": thread_id,
        "total_messages": len(messages),
        "transcript": format_thread_transcript(messages),
        "messages": structured_messages,
    }


@tool(
    name="gmail_get_message",
    description=("获取单封指定 ID 邮件的完整详情，包含正文文本、收发件人、主题和 Message-ID。"),
    side_effect=SideEffect.READONLY,
)
def get_email_detail(
    message_id: str,
    *,
    gmail: BaseGmailClient,
) -> dict[str, Any]:
    """获取单封邮件完整信息。"""
    msg = gmail.get_message(message_id)

    return {
        "id": msg.id,
        "thread_id": msg.thread_id,
        "rfc_message_id": msg.rfc_message_id,
        "from": msg.from_addr,
        "to": msg.to_addrs,
        "cc": msg.cc_addrs,
        "subject": msg.subject,
        "snippet": msg.snippet,
        "body": msg.body_text,
        "date": msg.date,
    }


@tool(
    name="gmail_prepare_reply",
    description=(
        "拟定邮件回复草稿并保存为待审阅预览。"
        "注意：此工具仅在本地生成待确认草稿，绝对不会真实发送邮件；"
        "真实发送必须在用户通过界面明确确认后，由系统执行。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
    emits_draft_saved=True,
)
def prepare_reply(
    source_message_id: str,
    thread_id: str,
    to: list[str],
    subject: str,
    body: str,
    *,
    task_id: str,
    drafts: ReplyDraftStore,
) -> dict[str, Any]:
    """拟定邮件回复草稿并持久化，返回操作标识与审阅状态。

    校验只在存储里做一次：按契约 §4，原邮件已有回复操作时先复用并返回其当前版本与状态，
    本次候选内容不参与校验，也不覆盖已保存内容。校验不通过时抛 DraftValidationError。
    """
    saved = drafts.save_reply_draft(
        task_id=task_id,
        source_message_id=source_message_id,
        thread_id=thread_id,
        to=to,
        subject=subject,
        body=body,
    )

    op_id = saved["operation_id"]
    version = saved["version"]
    status = saved["status"]

    if status == "pending":
        msg = (
            f"回复草稿已成功保存为待审阅状态（操作ID: {op_id}, 版本: {version}）。"
            "回复草稿已准备好，请审阅确认。"
        )
    else:
        msg = (
            f"原邮件已有回复记录（操作ID: {op_id}, 状态: {status}），"
            "已复用已有操作记录，未生成重复草稿。"
        )

    return {
        "operation_id": op_id,
        "version": version,
        "status": status,
        "message": msg,
    }


@tool(name="gmail_read_reply_draft", side_effect=SideEffect.READONLY)
def read_reply_draft(
    operation_id: str,
    *,
    task_id: str,
    drafts: ReplyDraftStore,
    tasks: SessionStore,
) -> dict:
    """读取当前任务的完整已保存草稿；修改前先读取最新内容与版本。"""
    if operation_id not in {op["operation_id"] for op in tasks.list_task_operations(task_id)}:
        # 不区分"不存在"与"属于别的任务"，不泄露其他任务的操作标识。
        raise NotFoundError(operation_id)
    return drafts.get_reply_draft(operation_id)


@tool(name="gmail_update_reply_draft", side_effect=SideEffect.LOCAL_WRITE, emits_draft_saved=True)
def update_reply_draft(
    operation_id: str,
    expected_version: int,
    to: list[str],
    subject: str,
    body: str,
    *,
    task_id: str,
    drafts: ReplyDraftStore,
    tasks: SessionStore,
) -> dict:
    """按用户修改意见保存完整新版本，不发送；必须使用刚读取的当前版本。"""
    read_reply_draft(operation_id, task_id=task_id, drafts=drafts, tasks=tasks)
    return drafts.update_reply_draft(operation_id, expected_version, to, subject, body)
