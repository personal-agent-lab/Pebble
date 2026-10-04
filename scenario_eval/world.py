"""有状态客户端替身；不接入真实邮箱、日历，不以世界状态判断场景通过。"""

import base64
import json
import shlex
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

from server.tools.calendar.client import CalDAVCalendarClient
from server.tools.gmail.client import (
    GmailAttachment,
    GmailAttachmentContent,
    GmailMessage,
    MimeParser,
    SendReplyResult,
)


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


class Clock:
    """仅评测进程的服务器时钟，单调等待和SDK网络时间仍使用真实时钟。"""

    def __init__(self, path):
        self.path = path

    def now(self):
        state = json.loads(self.path.read_text())
        return datetime.fromisoformat(state["at"]) + timedelta(
            seconds=datetime.now(UTC).timestamp() - state["wall"]
        )

    def set(self, at):
        value = datetime.fromisoformat(at)
        if value.tzinfo is None:
            raise ValueError("评测时间必须包含时区")
        save(self.path, {"at": value.isoformat(), "wall": datetime.now(UTC).timestamp()})

    def install(self):
        import sys

        clock = self

        class ControlledDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = clock.now()
                return value.astimezone(tz) if tz else value.astimezone().replace(tzinfo=None)

        # 仅替换服务器已导入的 datetime 引用，不改变SDK、asyncio与超时机制。
        for name, module in list(sys.modules.items()):
            if name.startswith("server.") and getattr(module, "datetime", None) is datetime:
                module.datetime = ControlledDateTime


class Mailbox:
    def __init__(self, root: Path, account: str, clock: Clock, faults=None):
        self.root, self.account, self.clock = root, account, clock
        self.messages, self.received, self.history = {}, {}, []
        self.attachments = {}
        self.faults = faults or {}
        self.lock = threading.RLock()
        self.persist()

    def persist(self):
        save(
            self.root / "mail.json",
            {
                "account": self.account,
                "messages": [asdict(m) for m in self.messages.values()],
                "received": self.received,
                "history": self.history,
            },
        )

    def deliver(self, fields):
        with self.lock:
            fields = dict(fields)
            fields.setdefault("labels", ["INBOX", "UNREAD"])
            fields.setdefault("received_at", self.clock.now().isoformat())
            fields.setdefault("snippet", fields["body_text"][:100])
            fields["attachments"] = [GmailAttachment(**a) for a in fields.get("attachments", [])]
            message = GmailMessage(**fields)
            self.messages[message.id] = message
            self.history.append(
                {
                    "id": str(len(self.history) + 1),
                    "messagesAdded": [
                        {
                            "message": {
                                "id": message.id,
                                "threadId": message.thread_id,
                                "labelIds": message.labels,
                            }
                        }
                    ],
                }
            )
            self.persist()
            return message

    def get_profile(self):
        with self.lock:
            return {"emailAddress": self.account, "historyId": str(len(self.history))}

    def list_added_messages(self, history_id, page_token=None):
        with self.lock:
            return {"history": self.history[int(history_id) :], "historyId": str(len(self.history))}

    def get_message(self, message_id):
        return self.messages[message_id]

    def get_thread(self, thread_id):
        with self.lock:
            return [m for m in self.messages.values() if m.thread_id == thread_id]

    def search_messages(self, query, max_results=10):
        tokens = shlex.split(query.lower())
        with self.lock:
            messages = list(self.messages.values())

        def matches(m):
            text = f"{m.subject} {m.body_text} {m.from_addr} {' '.join(m.to_addrs)}".lower()
            for term in tokens:
                if term.startswith("rfc822msgid:"):
                    if m.rfc_message_id.strip("<>").lower() != term[12:].strip("<>"):
                        return False
                elif term.startswith(("in:", "label:")):
                    label = term.split(":", 1)[1].upper()
                    if label not in m.labels:
                        return False
                elif term.startswith(("from:", "to:", "subject:")):
                    key, val = term.split(":", 1)
                    field = {"from": m.from_addr, "to": " ".join(m.to_addrs), "subject": m.subject}[
                        key
                    ]
                    if val not in field.lower():
                        return False
                elif term not in text:
                    return False
            return True

        return [
            {"id": m.id, "threadId": m.thread_id} for m in messages if matches(m)
        ][:max_results]

    def get_attachment(self, message_id, attachment_id):
        message = self.get_message(message_id)
        metadata = next(a for a in message.attachments if a.attachment_id == attachment_id)
        return GmailAttachmentContent(
            **asdict(metadata), data=self.attachments[(message_id, attachment_id)]
        )

    def send_raw_message(self, raw, thread_id=None):
        with self.lock:
            mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
            body = mime.get_body(preferencelist=("plain",)).get_content()
            message_id = f"sent-{len(self.messages) + 1}"
            message = GmailMessage(
                id=message_id,
                thread_id=thread_id or message_id,
                rfc_message_id=str(mime["Message-ID"]),
                from_addr=self.account,
                to_addrs=MimeParser.parse_address_list(str(mime["To"])),
                subject=str(mime["Subject"]),
                snippet=body[:100],
                body_text=body.replace("\r\n", "\n").rstrip("\n"),
                labels=["SENT"],
                received_at=self.clock.now().isoformat(),
                in_reply_to=str(mime.get("In-Reply-To", "")),
                references=str(mime.get("References", "")),
            )
            self.messages[message_id] = message
            for address in message.to_addrs:
                self.received.setdefault(address, []).append(asdict(message))
            self.persist()
            if self.faults.get("send_response_lost"):
                raise TimeoutError("虚拟发送已落地，响应丢失")
            return SendReplyResult(message_id, message.thread_id)


class Calendar:
    account = "virtual@example.test"
    calendar_url = "https://virtual.icloud.com/primary/"
    conflict_window = staticmethod(CalDAVCalendarClient.conflict_window)

    def __init__(self, root, events, clock, faults=None):
        self.root, self.clock = root, clock
        self.events = {e["event_id"]: e for e in events}
        self.faults = faults or {}
        self.lock = threading.RLock()
        self.persist()

    def persist(self):
        save(self.root / "calendar.json", {"events": list(self.events.values())})

    def list_events(self, time_min, time_max, calendar_id="primary", max_results=50):
        if self.faults.get("calendar_query_failed"):
            raise RuntimeError("虚拟日历查询失败")
        lower, upper = datetime.fromisoformat(time_min), datetime.fromisoformat(time_max)

        def instant(raw):
            value = datetime.fromisoformat(raw)
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        with self.lock:
            return {
                "events": [
                    dict(e)
                    for e in self.events.values()
                    if instant(e["start"]) < upper and instant(e["end"]) > lower
                ][:max_results]
            }

    def get_event(self, event_id):
        return dict(self.events[event_id])

    def check_conflicts(self, start, end, calendar_id="primary"):
        events = self.list_events(start, end, calendar_id)["events"]
        return {"conflicts": events}

    def create_event(self, *, operation_id, version, fields):
        with self.lock:
            if self.faults.get("calendar_create_failed"):
                return {"status": "failed", "reason": "虚拟创建失败"}
            uid = f"virtual-{operation_id}"
            self.events.setdefault(
                uid, {**fields, "event_id": uid, "updated_at": self.clock.now().isoformat()}
            )
            self.persist()
            if self.faults.get("calendar_response_lost"):
                return {"status": "unknown", "reason": "虚拟写入后响应丢失"}
            return {"status": "created", "event_id": uid}

    def verify_event(self, *, operation_id, fields):
        uid = f"virtual-{operation_id}"
        event = self.events.get(uid)
        if event and all(event.get(k) == v for k, v in fields.items()):
            return {"status": "created", "event_id": uid}
        return {"status": "unknown", "reason": "虚拟日历未核实"}
