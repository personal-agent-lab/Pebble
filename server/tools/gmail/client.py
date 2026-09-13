"""Gmail 协议客户端与认证封装。

提供真实 Google API 客户端实现与用于无凭证/测试环境的模拟桩客户端实现。
支持多层嵌套 MIME 报文解析、RFC 2047 Header 自动解码、HTML 降级清洗与线程按时间排序。
"""

from __future__ import annotations

import base64
import html
import logging
import re
import uuid
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import EmailMessage
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
            cc_addrs=[],
            body_html=(
                "<p>你好，诚邀你参加下周二下午 2 点的项目进展评审会议，"
                "地点在第二会议室，讨论 Pebble 邮件链路设计，请确认是否能够出席。</p>"
            ),
            internal_date_ms=1789200000000,
            labels=["INBOX", "UNREAD"],
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

    def send_raw_message(self, raw: str, thread_id: str) -> SendReplyResult:
        from dataclasses import replace
        from email import policy
        from email.parser import BytesParser

        mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        result = self.raw_send_reply(
            MimeParser.parse_address_list(str(mime["To"])),
            str(mime["Subject"]),
            mime.get_content(),
            thread_id,
            str(mime["In-Reply-To"]),
        )
        self.messages[result.message_id] = replace(
            self.messages[result.message_id],
            rfc_message_id=str(mime["Message-ID"]),
            in_reply_to=str(mime["In-Reply-To"]),
            references=str(mime["References"]),
        )
        return result

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
            cc_addrs=[],
            body_html=f"<p>{html.escape(body)}</p>",
            internal_date_ms=1789200300000,
            labels=["SENT"],
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
