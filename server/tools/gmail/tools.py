"""Agent 可见的 Gmail 查询、附件读取与邮件草稿工具。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from server.errors import NotFoundError
from server.sessions.service import SessionStore
from server.tools.gmail.client import BaseGmailClient, GmailAttachment, GmailMessage
from server.tools.gmail.service import MailDraftStore
from server.tools.registry import SideEffect, ToolFileResult, tool


def attachment_fields(attachment: GmailAttachment) -> dict[str, Any]:
    return {
        "attachment_id": attachment.attachment_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": attachment.size,
    }


def message_fields(message: GmailMessage, *, include_body: bool) -> dict[str, Any]:
    result = {
        "message_id": message.id,
        "thread_id": message.thread_id,
        "rfc_message_id": message.rfc_message_id,
        "from": message.from_addr,
        "to": message.to_addrs,
        "cc": message.cc_addrs,
        "subject": message.subject,
        "snippet": message.snippet,
        "received_at": message.received_at,
        "attachments": [attachment_fields(item) for item in message.attachments],
    }
    if include_body:
        result["body"] = message.body_text
    return result


@tool(
    name="gmail_search",
    description=("使用 Gmail 搜索条件查找邮件。返回每封邮件统一的基本信息和附件列表。"),
    side_effect=SideEffect.READONLY,
)
def search_emails(
    query: str, max_results: int = 10, *, gmail: BaseGmailClient
) -> list[dict[str, Any]]:
    results = gmail.search_messages(query, max_results=max_results)
    return [message_fields(gmail.get_message(item["id"]), include_body=False) for item in results]


@tool(
    name="gmail_get_thread",
    description="读取指定 Gmail 往来中按时间排列的全部邮件。",
    side_effect=SideEffect.READONLY,
)
def get_email_thread(thread_id: str, *, gmail: BaseGmailClient) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "messages": [
            message_fields(message, include_body=True) for message in gmail.get_thread(thread_id)
        ],
    }


@tool(
    name="gmail_get_message",
    description="读取单封 Gmail 邮件的完整正文、收发件人、时间和附件列表。",
    side_effect=SideEffect.READONLY,
)
def get_email_detail(message_id: str, *, gmail: BaseGmailClient) -> dict[str, Any]:
    return message_fields(gmail.get_message(message_id), include_body=True)


@tool(
    name="gmail_get_attachment",
    description="读取指定 Gmail 邮件中的一个附件，返回附件信息和原始文件。",
    side_effect=SideEffect.READONLY,
)
def get_attachment(
    message_id: str, attachment_id: str, *, gmail: BaseGmailClient
) -> ToolFileResult:
    attachment = gmail.get_attachment(message_id, attachment_id)
    metadata = {
        "message_id": message_id,
        "attachment_id": attachment.attachment_id,
        "size": attachment.size,
    }
    uri = (
        f"pebble://gmail/messages/{quote(message_id, safe='')}/attachments/"
        f"{quote(attachment.attachment_id, safe='')}/{quote(attachment.filename, safe='')}"
    )
    return ToolFileResult(
        uri=uri,
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        data=attachment.data,
        metadata=metadata,
    )


@tool(
    name="gmail_prepare_reply",
    description=(
        "针对一封 Gmail 邮件保存本地回复草稿，仅用于审阅，不会发送。"
        "邮件往来标识由工具从原邮件读取。attachment_ids 使用用户本轮上传的文件标识；"
        "没有附件时传空数组。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
    emits_draft_saved=True,
)
def prepare_reply(
    source_message_id: str,
    to: list[str],
    subject: str,
    body: str,
    attachment_ids: list[str],
    *,
    task_id: str,
    drafts: MailDraftStore,
    gmail: BaseGmailClient,
) -> dict[str, Any]:
    source = gmail.get_message(source_message_id)
    return drafts.save_reply_draft(
        task_id=task_id,
        source_message_id=source_message_id,
        thread_id=source.thread_id,
        to=to,
        subject=subject,
        body=body,
        attachment_ids=attachment_ids,
    )


@tool(
    name="gmail_prepare_email",
    description=(
        "保存一封不依赖已有邮件的本地新邮件草稿，仅用于审阅，不会发送。"
        "attachment_ids 使用用户本轮上传的文件标识；没有附件时传空数组。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
    emits_draft_saved=True,
)
def prepare_email(
    to: list[str],
    subject: str,
    body: str,
    attachment_ids: list[str],
    *,
    task_id: str,
    drafts: MailDraftStore,
) -> dict[str, Any]:
    return drafts.save_email_draft(task_id, to, subject, body, attachment_ids)


@tool(name="gmail_read_draft", side_effect=SideEffect.READONLY)
def read_draft(
    operation_id: str, *, task_id: str, drafts: MailDraftStore, tasks: SessionStore
) -> dict:
    """读取当前任务的完整邮件草稿；修改前先读取最新内容与版本。"""
    if operation_id not in {item["operation_id"] for item in tasks.list_task_operations(task_id)}:
        raise NotFoundError(operation_id)
    return drafts.get_draft(operation_id)


@tool(
    name="gmail_update_draft",
    description=(
        "按用户修改意见保存邮件草稿的完整新版本，不发送。保留附件时必须回传其 file_id；"
        "没有附件时传空数组。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
    emits_draft_saved=True,
)
def update_draft(
    operation_id: str,
    expected_version: int,
    to: list[str],
    subject: str,
    body: str,
    attachment_ids: list[str],
    *,
    task_id: str,
    drafts: MailDraftStore,
    tasks: SessionStore,
) -> dict:
    read_draft(operation_id, task_id=task_id, drafts=drafts, tasks=tasks)
    return drafts.update_draft(operation_id, expected_version, to, subject, body, attachment_ids)
