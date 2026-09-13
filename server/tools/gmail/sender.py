"""仅供 A 的审批控制器调用；调用前须原子取得执行权并读取确认版本。

A 必须持久化 sending/sent/failed/unknown，同一版本不可再次投递。
本模块不注册 MCP，不自动重试；未知结果只读核实。
"""

from __future__ import annotations

import base64
import hashlib
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from server.tools.gmail.client import BaseGmailClient
from server.tools.gmail.service import validate_reply_draft


def _message_id(source_message_id: str) -> str:
    # 按原邮件全局去重，与草稿协议一致；重启后仍可独立核实。
    digest = hashlib.sha256(source_message_id.encode()).hexdigest()
    return f"<pebble-reply-{digest}@pebble.local>"


def verify_reply_status(
    thread_id: str,
    source_message_id: str,
    *,
    client: BaseGmailClient,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """匹配系统生成的 Message-ID、SENT 标签和原邮件关联，绝不以主题猜测成功。"""
    try:
        source = client.get_message(source_message_id)
        for message in reversed(client.get_thread(thread_id)):
            if (
                message.thread_id != thread_id
                or "SENT" not in message.labels
                or message.rfc_message_id != _message_id(source_message_id)
                or message.in_reply_to != source.rfc_message_id
            ):
                continue
            if expected and (
                message.subject != expected["subject"]
                or message.to_addrs != expected["to"]
                or message.body_text.replace("\r\n", "\n").rstrip("\n")
                != expected["body"].replace("\r\n", "\n").rstrip("\n")
            ):
                continue
            if message.id:
                return {"status": "sent", "message_id": message.id}
    except Exception:
        pass  # 核实失败不能证明未发送，也不能泄露认证或服务端异常内容。
    return {"status": "unknown", "reason": "发送超时，待核实"}


def send_reply(
    operation_id: str,
    version: int,
    source_message_id: str,
    thread_id: str,
    to: list[str],
    subject: str,
    body: str,
    *,
    client: BaseGmailClient,
) -> dict[str, Any]:
    """直接发送 A 已确认的持久化字段；不经过 LLM，不在此推断用户确认。"""
    validation = validate_reply_draft(source_message_id, thread_id, to, subject, body)
    if not operation_id or type(version) is not int or version < 1 or not validation["valid"]:
        return {"status": "failed", "reason": "已确认草稿字段不合法"}
    try:
        source = client.get_message(source_message_id)
        if source.thread_id != thread_id or not source.rfc_message_id:
            return {"status": "failed", "reason": "原邮件线程不匹配或缺少 Message-ID"}
        mime = EmailMessage(policy=SMTP)
        mime["To"] = ", ".join(to)
        mime["Subject"] = subject
        mime["Message-ID"] = _message_id(source_message_id)
        mime["In-Reply-To"] = source.rfc_message_id
        mime["References"] = " ".join(filter(None, [source.references, source.rfc_message_id]))
        # MIMEText/set_content 会自动附加换行；直接编码保持确认正文内容。
        mime.set_payload(body.encode("utf-8"))
        mime["Content-Type"] = 'text/plain; charset="utf-8"'
        mime["MIME-Version"] = "1.0"
        from email.encoders import encode_base64

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
    except HttpError as exc:
        if 400 <= exc.resp.status < 500 and exc.resp.status != 408:
            return {"status": "failed", "reason": f"Gmail 拒绝发送（HTTP {exc.resp.status}）"}
    except Exception:
        pass  # 超时、断连、5xx 或无法分类的投递异常均不能安全重试。
    return verify_reply_status(
        thread_id,
        source_message_id,
        client=client,
        expected={"to": to, "subject": subject, "body": body},
    )
