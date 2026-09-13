"""会话式 Agent 替身：读模拟邮箱、按关键词判断意图、调用真实草稿接口。

只为手动走通邮件流程而存在，不是模型：摘要和建议取自邮件文件，改写靠固定规则。
草稿的保存、版本、状态与确认执行全部走服务端真实接口，所以页面上看到的版本变化、
版本冲突和发送结果都是真的。真实实现由 B 用 Qoder Agent SDK 提供，替身不进入默认装配。
"""

import asyncio
import json
import os
from pathlib import Path

from tests.support.agent_double import FakeAgentGateway
from tests.support.mailbox import MockMailbox

# 关键词判断够用即可：替身的目的是让每一步有稳定可复现的输入，不是模拟模型的理解。
DRAFT_WORDS = ("回信", "回复", "回一封", "回一下", "草稿", "答复", "写信", "写一封")
REVISE_WORDS = ("改", "修改", "调整", "补充", "加", "再", "换", "润色", "重写", "正式", "语气")

LINK_LINE = "另外，方便的话请把会议链接发我。"

CLOSINGS = ("谢谢", "顺颂", "多谢", "此致")


def chunk_delay() -> float:
    return float(os.environ.get("PEBBLE_TEST_AGENT_DELAY", "0.4"))


def matches(message: str, words: tuple[str, ...]) -> bool:
    return any(word in message for word in words)


GREETING_ENDINGS = ("，", "：", ",", ":", "!", "！")


def session_of(task_id: str) -> str:
    """会话标识跟着任务走：重启后新任务不会撞上旧任务的历史。"""
    return f"mock-session-{task_id[:8]}"


def summary(mail: dict) -> str:
    """邮件文件写了摘要就用它；现写的邮件没写，就退回正文里第一句实际内容。"""
    if mail.get("summary"):
        return mail["summary"]
    lines = [line.strip() for line in mail["body"].splitlines() if line.strip()]
    # 跳过「你好，」这类称呼，摘要才有信息量。
    content = [line for line in lines if not (len(line) <= 8 and line.endswith(GREETING_ENDINGS))]
    first = (content or lines or [""])[0]
    return first if len(first) <= 80 else first[:80] + "…"


def suggestion(mail: dict) -> str:
    if mail.get("suggestion"):
        return mail["suggestion"]
    return "需要我起草回信就说一声。"


def add_paragraph(body: str, line: str) -> str:
    """把新的一段插到落款之前；没有落款就直接追加，保持正文读起来是一封信。"""
    paragraphs = body.rstrip().split("\n\n")
    if len(paragraphs) > 1 and paragraphs[-1].startswith(CLOSINGS):
        paragraphs.insert(-1, line)
    else:
        paragraphs.append(line)
    return "\n\n".join(paragraphs)


def revise(body: str, message: str) -> str:
    """按用户要求改写正文；规则之外的要求原样补一段，改动在页面上可见。"""
    if "链接" in message:
        return body if LINK_LINE in body else add_paragraph(body, LINK_LINE)
    if "正式" in message:
        formal = body.replace("您好，", "尊敬的先生／女士：").replace("谢谢！", "顺颂商祺。")
        return formal if formal != body else "尊敬的先生／女士：\n\n" + body
    return add_paragraph(body, "补充：" + message)


class MockAgent(FakeAgentGateway):
    """按任务记住对应邮件，并把三类输入变成事件流。"""

    def __init__(self, mailbox: MockMailbox, *, state_dir: Path | None = None):
        super().__init__(session_prefix="mock")
        self.mailbox = mailbox
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.app = None
        self._mails: dict[str, str] = {}
        self.handle("new_mail", self._on_new_mail)
        self.handle("message", self._on_message)
        self.handle("execution_result", self._on_execution_result)

    def bind(self, app) -> None:
        """装配后绑定应用，替身通过 app.state 使用真实的任务与草稿服务。"""
        self.app = app

    # ---------- 替身自己的记忆 ----------

    def _file(self, name: str) -> Path | None:
        if self.state_dir is None:
            return None
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir / name

    def _remember_mail(self, task_id: str, message_id: str) -> None:
        self._mails[task_id] = message_id
        path = self._file("mock_agent_mails.json")
        if path is not None:
            path.write_text(json.dumps(self._mails, ensure_ascii=False), encoding="utf-8")

    def _mail_of(self, task_id: str) -> dict | None:
        """任务对应的邮件；重启后从替身自己的记录恢复，页面上的对话不至于断片。"""
        if task_id not in self._mails:
            path = self._file("mock_agent_mails.json")
            if path is not None and path.exists():
                self._mails.update(json.loads(path.read_text(encoding="utf-8")))
        message_id = self._mails.get(task_id)
        return self.mailbox.read(message_id) if message_id is not None else None

    def _remember(self, session_id: str | None, messages: list[dict]) -> None:
        super()._remember(session_id, messages)
        path = self._file("mock_agent_history.jsonl")
        if path is None or session_id is None or not messages:
            return
        with path.open("a", encoding="utf-8") as output:
            for message in messages:
                record = {"sdk_session_id": session_id, **message}
                output.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def read_history(self, *, task_id: str, sdk_session_id: str | None) -> list[dict]:
        path = self._file("mock_agent_history.jsonl")
        if path is None or not path.exists():
            return await super().read_history(task_id=task_id, sdk_session_id=sdk_session_id)
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record["sdk_session_id"] == sdk_session_id:
                messages.append({"role": record["role"], "text": record["text"]})
        return messages

    # ---------- 事件脚本 ----------

    async def _say(self, text: str):
        await asyncio.sleep(chunk_delay())
        return {"type": "text", "text": text}

    def _reply_operation(self, task_id: str) -> dict | None:
        operations = self.app.state.tasks.list_task_operations(task_id)
        replies = [item for item in operations if item["type"] == "mail_reply"]
        return replies[0] if replies else None

    async def _on_new_mail(self, *, task_id, sdk_session_id, source_message_id, thread_id):
        yield {"type": "session", "sdk_session_id": sdk_session_id or session_of(task_id)}
        self._remember_mail(task_id, source_message_id)
        mail = self.mailbox.read(source_message_id)
        if mail is None:
            yield await self._say("收到一封新邮件，但读不到内容。")
            yield {"type": "done"}
            return
        # 分两段发：页面上能看到事件流逐段到达，历史里也是两条干净的消息。
        yield await self._say(
            f"新邮件来自 {mail['from']}，主题《{mail['subject']}》。\n\n摘要：{summary(mail)}"
        )
        yield await self._say(suggestion(mail))
        yield {"type": "done"}

    async def _on_message(self, *, task_id, sdk_session_id, message):
        yield {"type": "session", "sdk_session_id": sdk_session_id or session_of(task_id)}
        mail = self._mail_of(task_id)
        if mail is None:
            yield await self._say("这个任务还没有关联邮件，替身只能闲聊，起草回信要从新邮件开始。")
            yield {"type": "done"}
            return

        operation = self._reply_operation(task_id)
        wants_draft = matches(message, DRAFT_WORDS) or matches(message, REVISE_WORDS)
        if operation is None and not wants_draft:
            yield await self._say(
                f"这封邮件来自 {mail['from']}，主题《{mail['subject']}》。{summary(mail)}"
                f"\n\n{suggestion(mail)}"
            )
            yield {"type": "done"}
            return

        if operation is not None and operation["status"] != "pending":
            yield await self._say(
                f"这封回信已经是 {operation['status']} 状态，不能再改。要继续沟通得发新邮件。"
            )
            yield {"type": "done"}
            return

        if operation is None:
            yield await self._say("好的，我按邮件内容拟一版回信。")
            saved = self.app.state.drafts.save_reply_draft(
                task_id,
                mail["message_id"],
                mail["thread_id"],
                [mail["from"]],
                "回复：" + mail["subject"],
                mail.get("reply_body")
                or f"您好，\n\n已收到您关于{mail['subject']}的邮件。\n\n谢谢！",
            )
            note = "草稿已存好，在待确认里可以看全文；确认发送由你来点。"
        else:
            yield await self._say("好的，我按你的要求改一版。")
            current = self.app.state.drafts.get_reply_draft(operation["operation_id"])
            saved = self.app.state.drafts.update_reply_draft(
                operation["operation_id"],
                current["version"],
                current["to"],
                current["subject"],
                revise(current["body"], message),
            )
            note = "改好了，请再看看。"

        yield {
            "type": "draft_saved",
            "operation_id": saved["operation_id"],
            "version": saved["version"],
        }
        yield await self._say(note)
        yield {"type": "done"}

    async def _on_execution_result(self, *, task_id, sdk_session_id, operation_id, version, result):
        yield {"type": "session", "sdk_session_id": sdk_session_id}
        status = result["status"]
        if status == "sent":
            text = "回信已经发出去了。还需要我做什么？"
        elif status == "failed":
            text = f"回信没能发出去：{result['reason']}。我不会自动重试。"
        else:
            text = f"发送结果还不确定：{result['reason']}。在核实清楚之前不会重复发送。"
        yield await self._say(text)
        yield {"type": "done"}
