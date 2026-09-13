"""Gmail 协议客户端与认证封装。

只有真实 Google API 实现；测试替身在 `tests/support/gmail_double.py`，不随服务发布。
支持多层嵌套 MIME 报文解析、RFC 2047 Header 自动解码、HTML 降级清洗与线程按时间排序。
"""

from __future__ import annotations

import base64
import html
import logging
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import getaddresses
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
    """结构化的 Gmail 邮件对象，保持对未来字段（如抄送、HTML正文、标签、时间戳）的可扩展性。"""

    id: str
    thread_id: str
    rfc_message_id: str
    from_addr: str
    to_addrs: list[str]
    subject: str
    snippet: str
    body_text: str
    date: str
    cc_addrs: list[str] = field(default_factory=list)
    body_html: str = ""
    internal_date_ms: int = 0
    labels: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    references: str = ""


@dataclass(frozen=True)
class SendReplyResult:
    message_id: str
    thread_id: str


class BaseGmailClient(Protocol):
    """Gmail 客户端抽象协议。"""

    def send_raw_message(self, raw: str, thread_id: str) -> SendReplyResult:
        """发送已构建的 MIME；仅供内部审批执行器调用。"""
        ...

    def get_message(self, message_id: str) -> GmailMessage:
        """获取指定 ID 的单封邮件详情。"""
        ...

    def get_thread(self, thread_id: str) -> list[GmailMessage]:
        """获取指定线程内的全部往来邮件，按时间正序排序。"""
        ...

    def search_messages(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        """按搜索词查询邮件列表。"""
        ...


class MimeParser:
    """可复用的 MIME 邮件解析与清洗工具类。"""

    @staticmethod
    def safe_b64decode(data: str | None) -> str:
        """安全解码 Gmail 的 base64url 数据，自动补齐缺失的 '=' padding。"""
        if not data:
            return ""
        padded = data + "=" * (-len(data) % 4)
        try:
            raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
            return raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def decode_header_value(value: str) -> str:
        """将 RFC 2047 格式的编码字符串（如 =?utf-8?b?...?=）还原为 unicode。"""
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def parse_headers(cls, headers_list: list[dict[str, str]]) -> dict[str, str]:
        """提取所有邮件头，键转小写，值自动完成编码还原。"""
        headers: dict[str, str] = {}
        for h in headers_list:
            key = h.get("name", "").lower()
            val = cls.decode_header_value(h.get("value", ""))
            headers[key] = val
        return headers

    @staticmethod
    def parse_address_list(address_header: str) -> list[str]:
        """安全解析收件人/抄送列表，兼容 'Name <email>' 与纯逗号分隔格式。"""
        if not address_header:
            return []
        parsed = getaddresses([address_header])
        clean_addresses: list[str] = []
        for _realname, addr in parsed:
            addr_str = addr.strip()
            if addr_str:
                clean_addresses.append(addr_str)
        if not clean_addresses:
            clean_addresses = [a.strip() for a in address_header.split(",") if a.strip()]
        return clean_addresses

    @staticmethod
    def strip_html_tags(html_content: str) -> str:
        """清洗 HTML 内容为供 Agent 阅读的纯文本。"""
        if not html_content:
            return ""
        text = re.sub(r"<(br|p|div|tr|h\d)[^>]*>", "\n", html_content, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text).replace("\xa0", " ")
        return re.sub(r"\n\s*\n", "\n\n", text).strip()

    @classmethod
    def extract_body_parts(cls, payload: dict[str, Any]) -> tuple[str, str]:
        """递归遍历 MIME 树，提取 (plain_text, html_text)。

        排除附件与非文本 parts，优先提供真实 text/plain；若缺失则降级从 HTML 清洗生成。
        """
        plain_parts: list[str] = []
        html_parts: list[str] = []

        def _walk(part: dict[str, Any]) -> None:
            mime_type = part.get("mimeType", "").lower()
            data = part.get("body", {}).get("data")

            if mime_type == "text/plain" and data:
                text = cls.safe_b64decode(data)
                if text:
                    plain_parts.append(text)
            elif mime_type == "text/html" and data:
                html_text = cls.safe_b64decode(data)
                if html_text:
                    html_parts.append(html_text)

            for sub_part in part.get("parts", []):
                _walk(sub_part)

        _walk(payload)

        plain_body = "\n\n".join(plain_parts).strip()
        html_body = "\n\n".join(html_parts).strip()

        if not plain_body and html_body:
            plain_body = cls.strip_html_tags(html_body)

        return plain_body, html_body


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

    @classmethod
    def _parse_message_dict(cls, raw: dict[str, Any]) -> GmailMessage:
        msg_id = raw.get("id", "")
        thread_id = raw.get("threadId", "")
        snippet = raw.get("snippet", "")
        internal_date_raw = raw.get("internalDate", "0")
        internal_date_ms = int(internal_date_raw) if str(internal_date_raw).isdigit() else 0
        labels = raw.get("labelIds", [])

        payload = raw.get("payload", {})
        headers = MimeParser.parse_headers(payload.get("headers", []))

        rfc_message_id = headers.get("message-id", "")
        from_addr = headers.get("from", "")
        to_addrs = MimeParser.parse_address_list(headers.get("to", ""))
        cc_addrs = MimeParser.parse_address_list(headers.get("cc", ""))
        subject = headers.get("subject", "")
        date = headers.get("date", "")

        body_text, body_html = MimeParser.extract_body_parts(payload)
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
            cc_addrs=cc_addrs,
            body_html=body_html,
            internal_date_ms=internal_date_ms,
            labels=labels,
            in_reply_to=headers.get("in-reply-to", ""),
            references=headers.get("references", ""),
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

        # 按毫秒时间戳正序排列，确保上下文往来顺序准确
        messages_raw.sort(
            key=lambda m: (
                int(m.get("internalDate", 0)) if str(m.get("internalDate", "")).isdigit() else 0
            )
        )
        return [self._parse_message_dict(m) for m in messages_raw]

    def search_messages(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        service = self.get_service()
        res = (
            service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        )
        return [{"id": m["id"], "threadId": m["threadId"]} for m in res.get("messages", [])]

    def send_raw_message(self, raw: str, thread_id: str) -> SendReplyResult:
        result = (
            self.get_service()
            .users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": thread_id})
            .execute(num_retries=0)
        )
        return SendReplyResult(result.get("id", ""), result.get("threadId", thread_id))


def create_gmail_client(settings: Settings | None = None) -> GoogleApiGmailClient:
    """装配期构造真实 Gmail 客户端；测试须显式注入客户端，不在调用点回退。"""
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

    raise RuntimeError("未配置 Gmail OAuth 凭证")
