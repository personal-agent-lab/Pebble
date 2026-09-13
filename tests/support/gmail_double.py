"""Gmail 的测试替身：内存邮箱与发送记录，不连接真实邮箱。校验用真实纯函数，不设替身。

`MockGmailClient` 是 `BaseGmailClient` 的内存实现，供工具、发送与联调测试显式注入；
`send` 把发送参数写进 sent.jsonl，供可启动的验收后端使用。

发送结果由 `PEBBLE_TEST_SEND_STATUS` 指定（`sent`、`failed`、`unknown`），
`PEBBLE_TEST_SEND_DELAY` 给发送加秒级延时，便于在页面上看到 sending 中间态。
"""

import base64
import html
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.config import get_settings
from server.tools.gmail.client import (
    BaseGmailClient,
    GmailMessage,
    MimeParser,
    SendReplyResult,
)


def sent_log() -> Path:
    return get_settings().data_dir / "sent.jsonl"


def send(**fields):
    with sent_log().open("a") as output:
        output.write(json.dumps(fields, ensure_ascii=False) + "\n")
    time.sleep(float(os.environ.get("PEBBLE_TEST_SEND_DELAY", "0")))
    status = os.environ.get("PEBBLE_TEST_SEND_STATUS", "sent")
    if status == "sent":
        return {"status": status, "message_id": "test-message"}
    return {"status": status, "reason": "测试替身模拟结果：" + status}


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
