"""只供 Confirmation 调用的邮件发送与结果核实。"""

from __future__ import annotations

import base64
import hashlib
from email.encoders import encode_base64
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from server.tools.gmail.client import BaseGmailClient, GmailMessage
from server.tools.gmail.service import DraftKind, validate_mail_draft

UNVERIFIED_REASON = "未查到匹配的已发送邮件，结果仍待核实"


def _message_id(operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode()).hexdigest()
    return f"<pebble-mail-{digest}@pebble.local>"


def _same_content(message: GmailMessage, *, to: list[str], subject: str, body: str) -> bool:
    return (
        message.to_addrs == to
        and message.subject == subject
        and message.body_text.replace("\r\n", "\n").rstrip("\n")
        == body.replace("\r\n", "\n").rstrip("\n")
    )


def verify_message(
    *,
    operation_id: str,
    kind: DraftKind,
    to: list[str],
    subject: str,
    body: str,
    client: BaseGmailClient,
    source_message_id: str | None = None,
    thread_id: str | None = None,
    reason: str = UNVERIFIED_REASON,
) -> dict[str, Any]:
    """只读核实已确认版本是否发出；查不到不证明发送失败。"""
    expected_id = _message_id(operation_id)
    try:
        source = client.get_message(source_message_id) if kind == "reply" else None
        for match in client.search_messages(f"rfc822msgid:{expected_id}", max_results=10):
            message = client.get_message(match["id"])
            if (
                "SENT" not in message.labels
                or message.rfc_message_id != expected_id
                or not _same_content(message, to=to, subject=subject, body=body)
            ):
                continue
            if kind == "reply" and (
                source is None
                or message.thread_id != thread_id
                or message.in_reply_to != source.rfc_message_id
            ):
                continue
            if message.id:
                return {"status": "sent", "message_id": message.id}
    except Exception:
        pass
    return {"status": "unknown", "reason": reason}


def send_message(
    operation_id: str,
    version: int,
    kind: DraftKind,
    to: list[str],
    subject: str,
    body: str,
    *,
    client: BaseGmailClient,
    source_message_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """发送已确认的新邮件或回复；内容不经过模型再生成。"""
    validation = validate_mail_draft(
        kind=kind,
        source_message_id=source_message_id,
        thread_id=thread_id,
        to=to,
        subject=subject,
        body=body,
    )
    if not operation_id or type(version) is not int or version < 1 or not validation["valid"]:
        return {"status": "failed", "reason": "已确认草稿字段不合法"}
    try:
        source = client.get_message(source_message_id) if kind == "reply" else None
        if kind == "reply" and (
            source is None or source.thread_id != thread_id or not source.rfc_message_id
        ):
            return {"status": "failed", "reason": "原邮件往来不匹配或缺少 Message-ID"}
        mime = EmailMessage(policy=SMTP)
        mime["To"] = ", ".join(to)
        mime["Subject"] = subject
        mime["Message-ID"] = _message_id(operation_id)
        if source is not None:
            mime["In-Reply-To"] = source.rfc_message_id
            mime["References"] = " ".join(filter(None, [source.references, source.rfc_message_id]))
        mime.set_payload(body.encode("utf-8"))
        mime["Content-Type"] = 'text/plain; charset="utf-8"'
        mime["MIME-Version"] = "1.0"
        encode_base64(mime)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    except Exception:
        return {"status": "failed", "reason": "构建邮件失败或无法读取原邮件"}
    try:
        result = client.send_raw_message(raw, thread_id)
        if result.message_id:
            return {"status": "sent", "message_id": result.message_id}
    except RefreshError:
        return {"status": "failed", "reason": "Gmail 认证已过期"}
    except HttpError as error:
        if 400 <= error.resp.status < 500 and error.resp.status != 408:
            return {"status": "failed", "reason": f"Gmail 拒绝发送（HTTP {error.resp.status}）"}
    except Exception:
        pass
    return verify_message(
        operation_id=operation_id,
        kind=kind,
        source_message_id=source_message_id,
        thread_id=thread_id,
        to=to,
        subject=subject,
        body=body,
        client=client,
        reason="发送调用未给出结果，待核实",
    )
