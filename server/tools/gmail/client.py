"""Gmail 协议客户端与认证封装。

提供真实 Google API 客户端实现与用于无凭证/测试环境的模拟桩客户端实现。
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from server.config import Settings, get_settings

logger = logging.getLogger(__name__)

GMAIL_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str
    rfc_message_id: str
    from_addr: str
    to_addrs: list[str]
    subject: str
    snippet: str
    body_text: str
    date: str


@dataclass(frozen=True)
class SendReplyResult:
    message_id: str
    thread_id: str


class BaseGmailClient(Protocol):
    """Gmail 客户端抽象协议。"""

    def get_message(self, message_id: str) -> GmailMessage:
        """获取指定 ID 的单封邮件详情。"""
        ...

    def get_thread(self, thread_id: str) -> list[GmailMessage]:
        """获取指定线程内的全部往来邮件，按时间排序。"""
        ...

    def search_messages(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        """按搜索词查询邮件列表。"""
        ...

    def raw_send_reply(
        self,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to_rfc_id: str | None = None,
    ) -> SendReplyResult:
        """构建 RFC 2822 邮件报文并发送回复。"""
        ...

    def verify_message_sent(self, thread_id: str, subject_keyword: str) -> bool:
        """检查特定线程中是否已存在已发送的匹配邮件，用于超时状态核实。"""
        ...


class GoogleApiGmailClient(BaseGmailClient):
    """使用真实 Google API Client 与 OAuth2 认证的 Gmail 实现。"""

    def __init__(
        self,
        credentials_path: Path,
        token_path: Path,
        service: Resource | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = service

    def get_service(self) -> Resource:
        if self._service is not None:
            return self._service

        creds: Credentials | None = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"Gmail OAuth 凭证文件不存在: {self.credentials_path}。"
                        "请在 Google Cloud Console 下载 credentials.json 并放置到该路径。"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    @staticmethod
    def _extract_body_text(payload: dict[str, Any]) -> str:
        """递归解析 MIME payload，优先提取 text/plain 正文。"""

        def _decode_body(data: str | None) -> str:
            if not data:
                return ""
            try:
                return base64.urlsafe_b64decode(data.encode("ascii")).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                return ""

        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")

        if mime_type == "text/plain" and body_data:
            text = _decode_body(body_data)
            if text:
                return text

        parts = payload.get("parts", [])
        # 第一遍优先查找 text/plain
        for part in parts:
            if part.get("mimeType") == "text/plain":
                text = _decode_body(part.get("body", {}).get("data"))
                if text:
                    return text

        # 第二遍递归查找子 parts
        for part in parts:
            nested_text = GoogleApiGmailClient._extract_body_text(part)
            if nested_text:
                return nested_text

        # 如果只有顶层直接数据
        if body_data:
            return _decode_body(body_data)

        return ""

    @classmethod
    def _parse_message_dict(cls, raw: dict[str, Any]) -> GmailMessage:
        msg_id = raw.get("id", "")
        thread_id = raw.get("threadId", "")
        snippet = raw.get("snippet", "")
        payload = raw.get("payload", {})
        headers_list = payload.get("headers", [])
        headers = {h.get("name", "").lower(): h.get("value", "") for h in headers_list}

        rfc_message_id = headers.get("message-id", "")
        from_addr = headers.get("from", "")
        to_header = headers.get("to", "")
        to_addrs = [addr.strip() for addr in to_header.split(",") if addr.strip()]
        subject = headers.get("subject", "")
        date = headers.get("date", "")

        body_text = cls._extract_body_text(payload)
        if not body_text:
            body_text = snippet

        return GmailMessage(
            id=msg_id,
            thread_id=thread_id,
            rfc_message_id=rfc_message_id,
            from_addr=from_addr,
            to_addrs=to_addrs,
            subject=subject,
            snippet=snippet,
            body_text=body_text,
            date=date,
        )

    def get_message(self, message_id: str) -> GmailMessage:
        service = self.get_service()
        raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
        return self._parse_message_dict(raw)

    def get_thread(self, thread_id: str) -> list[GmailMessage]:
        service = self.get_service()
        thread_raw = (
            service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        )
        messages_raw = thread_raw.get("messages", [])
        return [self._parse_message_dict(m) for m in messages_raw]

    def search_messages(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        service = self.get_service()
        res = (
            service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        )
        return [{"id": m["id"], "threadId": m["threadId"]} for m in res.get("messages", [])]

    def raw_send_reply(
        self,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to_rfc_id: str | None = None,
    ) -> SendReplyResult:
        mime_msg = EmailMessage()
        mime_msg["To"] = ", ".join(to)
        mime_msg["Subject"] = subject
        mime_msg.set_content(body)

        if in_reply_to_rfc_id:
            mime_msg["In-Reply-To"] = in_reply_to_rfc_id
            mime_msg["References"] = in_reply_to_rfc_id

        raw_bytes = mime_msg.as_bytes()
        raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii")

        body_payload = {
            "raw": raw_b64,
            "threadId": thread_id,
        }

        service = self.get_service()
        sent_res = service.users().messages().send(userId="me", body=body_payload).execute()
        sent_id = sent_res.get("id", "")
        sent_thread_id = sent_res.get("threadId", thread_id)
        return SendReplyResult(message_id=sent_id, thread_id=sent_thread_id)

    def verify_message_sent(self, thread_id: str, subject_keyword: str) -> bool:
        try:
            messages = self.get_thread(thread_id)
            normalized_keyword = subject_keyword.replace("Re: ", "").strip().lower()
            for msg in reversed(messages):
                msg_subject_norm = msg.subject.replace("Re: ", "").strip().lower()
                if normalized_keyword and normalized_keyword in msg_subject_norm:
                    return True
            return False
        except Exception as exc:
            logger.warning("核实邮件发送状态时异常: %s", exc)
            return False


@dataclass
class MockGmailClient(BaseGmailClient):
    """内存模拟的 Gmail 客户端，用于单测或未配置真实凭证时的本地开发联调。"""

    messages: dict[str, GmailMessage] = field(default_factory=dict)
    threads: dict[str, list[str]] = field(default_factory=dict)
    sent_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self._seed_default_messages()

    def _seed_default_messages(self) -> None:
        """内置一条测试用会议邀请邮件与线程。"""
        default_msg = GmailMessage(
            id="msg_invite_001",
            thread_id="thread_invite_001",
            rfc_message_id="<invite-001@example.com>",
            from_addr="Alice <alice@example.com>",
            to_addrs=["user@example.com"],
            subject="项目进展评审与架构讨论邀请",
            snippet="诚邀您参加下周二下午 2 点的项目进展评审会议...",
            body_text=(
                "你好，诚邀你参加下周二下午 2 点的项目进展评审会议，"
                "地点在第二会议室，讨论 Pebble 邮件链路设计，请确认是否能够出席。"
            ),
            date="2026-09-12 10:00:00",
        )
        self.messages[default_msg.id] = default_msg
        self.threads[default_msg.thread_id] = [default_msg.id]

    def get_message(self, message_id: str) -> GmailMessage:
        if message_id not in self.messages:
            raise KeyError(f"Mock 邮件未找到: {message_id}")
        return self.messages[message_id]

    def get_thread(self, thread_id: str) -> list[GmailMessage]:
        if thread_id not in self.threads:
            raise KeyError(f"Mock 线程未找到: {thread_id}")
        return [self.messages[mid] for mid in self.threads[thread_id] if mid in self.messages]

    def search_messages(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        q = query.lower()
        results: list[dict[str, str]] = []
        for msg in self.messages.values():
            if q in msg.subject.lower() or q in msg.body_text.lower():
                results.append({"id": msg.id, "threadId": msg.thread_id})
                if len(results) >= max_results:
                    break
        return results

    def raw_send_reply(
        self,
        to: list[str],
        subject: str,
        body: str,
        thread_id: str,
        in_reply_to_rfc_id: str | None = None,
    ) -> SendReplyResult:
        sent_id = f"mock_sent_{uuid.uuid4().hex[:8]}"
        new_msg = GmailMessage(
            id=sent_id,
            thread_id=thread_id,
            rfc_message_id=f"<{sent_id}@pebble.local>",
            from_addr="user@example.com",
            to_addrs=to,
            subject=subject,
            snippet=body[:50],
            body_text=body,
            date="2026-09-12 10:05:00",
        )
        self.messages[sent_id] = new_msg
        if thread_id in self.threads:
            self.threads[thread_id].append(sent_id)
        else:
            self.threads[thread_id] = [sent_id]

        self.sent_log.append(
            {
                "id": sent_id,
                "thread_id": thread_id,
                "to": to,
                "subject": subject,
                "body": body,
                "in_reply_to": in_reply_to_rfc_id,
            }
        )
        return SendReplyResult(message_id=sent_id, thread_id=thread_id)

    def verify_message_sent(self, thread_id: str, subject_keyword: str) -> bool:
        normalized_keyword = subject_keyword.replace("Re: ", "").strip().lower()
        for sent in self.sent_log:
            if sent["thread_id"] == thread_id:
                sent_subj_norm = sent["subject"].replace("Re: ", "").strip().lower()
                if normalized_keyword in sent_subj_norm:
                    return True
        return False


def get_gmail_client(settings: Settings | None = None) -> BaseGmailClient:
    """根据凭证文件是否存在，自动返回真实 Google API 客户端或模拟客户端。"""
    active_settings = settings or get_settings()
    has_creds = (
        active_settings.gmail_credentials_file.exists() or active_settings.gmail_token_file.exists()
    )

    if has_creds:
        logger.info(
            "加载真实 GmailClient，凭证路径: %s",
            active_settings.gmail_credentials_file,
        )
        return GoogleApiGmailClient(
            credentials_path=active_settings.gmail_credentials_file,
            token_path=active_settings.gmail_token_file,
        )

    logger.info("未检测到真实 Gmail 凭证，回退至 MockGmailClient")
    return MockGmailClient()
