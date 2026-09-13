"""Gmail 工具集：向 Agent 暴露的查询工具与草稿准备工具。

严格遵守安全红线：
- 查询工具标记为 READONLY，模型可自主调用；
- 外部写操作（发送邮件）不在此注册，模型不可见。
"""

from __future__ import annotations

from typing import Any

from server.tools.gmail.client import BaseGmailClient, get_gmail_client
from server.tools.gmail.protocol import DraftStorageProtocol, get_draft_storage
from server.tools.gmail.validator import validate_reply_draft
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
    client: BaseGmailClient | None = None,
) -> list[dict[str, Any]]:
    """搜索邮件列表并返回匹配的摘要信息。"""
    active_client = client or get_gmail_client()
    search_results = active_client.search_messages(query, max_results=max_results)

    email_summaries: list[dict[str, Any]] = []
    for item in search_results:
        msg_id = item["id"]
        try:
            msg = active_client.get_message(msg_id)
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
    client: BaseGmailClient | None = None,
) -> dict[str, Any]:
    """获取线程内全部往来邮件详情，生成时间线对话记录注入模型上下文。"""
    active_client = client or get_gmail_client()
    messages = active_client.get_thread(thread_id)

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
    client: BaseGmailClient | None = None,
) -> dict[str, Any]:
    """获取单封邮件完整信息。"""
    active_client = client or get_gmail_client()
    msg = active_client.get_message(message_id)

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
)
def prepare_reply(
    task_id: str,
    source_message_id: str,
    thread_id: str,
    to: list[str],
    subject: str,
    body: str,
    storage: DraftStorageProtocol | None = None,
) -> dict[str, Any]:
    """拟定邮件回复草稿，经过业务规则校验后持久化，返回操作标识与审阅状态。"""
    # 1. 核心复用：调用纯函数业务规则校验
    val_res = validate_reply_draft(
        source_message_id=source_message_id,
        thread_id=thread_id,
        to=to,
        subject=subject,
        body=body,
    )
    if not val_res["valid"]:
        return {
            "success": False,
            "error": "回复草稿校验失败，请检查并修正字段后重试",
            "validation_errors": val_res["errors"],
        }

    # 2. 校验通过，调用持久化协议保存草稿（由 A 提供，自带去重机制）
    active_storage = storage or get_draft_storage()
    saved = active_storage.save_reply_draft(
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
            "已向用户展示预览，等待用户在界面审阅并明确确认后才会发送。"
        )
    else:
        msg = (
            f"原邮件已有回复记录（操作ID: {op_id}, 状态: {status}），"
            "已复用已有操作记录，未生成重复草稿。"
        )

    return {
        "success": True,
        "operation_id": op_id,
        "version": version,
        "status": status,
        "message": msg,
    }
