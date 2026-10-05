"""会话材料注入：固定背景去重，变化与事件追加，压缩后重建。

只跟踪应用材料的摘要，不读取或改写 SDK 历史，也不承担 Agent 执行循环。
摘要是可删除的派生缓存；不确定是否成功送达时，下轮重新提供完整背景。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from qodercn_agent_sdk import HookMatcher

from server.agent.context import Material, render_material, render_materials

FULL_NOTE = (
    "以下是应用提供的完整当前背景与手动选择材料，替代此前的背景与手动选择。"
    "未列出的旧材料已清空或停用，不再按其内容执行；本轮输入与执行事件仍各自有效。"
)


class SessionMaterials:
    def __init__(self, path: Path, session_id: str | None):
        self.path = path
        self.session_id = session_id
        self.previous = self._read()
        self.pending = dict(self.previous)
        self.background: tuple[Material, ...] = ()
        self.selected: tuple[Material, ...] = ()
        self.events: tuple[Material, ...] = ()
        self.prompt_seen = False
        self.injected = False
        self.submitted: list[dict] = []
        self.observe: Callable[[list[dict]], None] | None = None

    def _read(self) -> dict[str, dict[str, str]]:
        if self.session_id is None:
            return {}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            hashes = state["hashes"]
            if (
                state["schema"] == 1
                and state["session_id"] == self.session_id
                and isinstance(hashes, dict)
                and all(
                    isinstance(k, str)
                    and isinstance(v, dict)
                    and isinstance(v.get("hash"), str)
                    and isinstance(v.get("title"), str)
                    for k, v in hashes.items()
                )
            ):
                return hashes
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {}

    def begin(self) -> None:
        # 先撤销持久化检查点：进程被杀或调用失败时，不能复用可能不完整的注入状态。
        self.path.unlink(missing_ok=True)

    def configure(self, *, background, selected=(), events=(), observe=None) -> None:
        self.background = tuple(background)
        self.selected = tuple(selected)
        self.events = tuple(events)
        self.observe = observe

    @staticmethod
    def _hash(material: Material) -> str:
        return hashlib.sha256(render_material(material).encode("utf-8")).hexdigest()

    def _record(self, items) -> None:
        self.submitted.extend(
            {"title": item.title, "chars": len(render_material(item))} for item in items
        )
        if self.observe is not None:
            self.observe(list(self.submitted))

    def _refresh(self, *, full: bool) -> str:
        current = (*self.background, *self.selected)
        hashes = {
            item.key or item.title: {"title": item.title, "hash": self._hash(item)}
            for item in current
        }
        changed = [
            item for item in current
            if full or self.pending.get(item.key or item.title) != hashes[item.key or item.title]
        ]
        removed = [
            Material(value["title"], "该材料已清空或停用，不再按此前内容执行。")
            for key, value in self.pending.items() if key not in hashes
        ]
        blocks = [FULL_NOTE] if full else []
        if changed or removed:
            if not full:
                blocks.append(
                    "以下是应用提供的当前有效材料。每份同名材料完整替代此前版本；"
                    "空内容表示该材料已清空，旧内容不再适用。"
                )
            blocks.extend(render_material(item) for item in changed)
            blocks.extend(render_material(item) for item in removed)
            if not full:
                blocks.append(
                    "当前手动选择材料仅包括："
                    + ("、".join(item.title for item in self.selected) or "无")
                    + "。此前未列出的手动选择不再适用。"
                )
        self._record([*changed, *removed])
        self.pending = hashes
        self.injected = True
        return "\n\n".join(blocks)

    @staticmethod
    def _output(event: str, text: str) -> dict:
        if not text:
            return {}
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

    def hooks(self) -> dict:
        async def start(data, _tool_use_id, _context):
            source = data.get("source", "startup")
            if source in {"startup", "clear", "compact"} or not self.previous:
                return self._output("SessionStart", self._refresh(full=True))
            return {}

        async def prompt(data, _tool_use_id, _context):
            # 应用轮前的 /compact 不是本轮任务输入，不能提前消耗事件材料。
            if data.get("prompt", "").strip() == "/compact":
                return {}
            text = self._refresh(full=not self.injected and not self.previous)
            if not self.prompt_seen:
                text = "\n\n".join(part for part in (text, render_materials(self.events)) if part)
                self._record(self.events)
                self.prompt_seen = True
            return self._output("UserPromptSubmit", text)

        async def compact(_data, _tool_use_id, _context):
            # 不依赖摘要保留全部背景；由 compact 的 SessionStart 或下个输入补齐。
            self.previous = {}
            self.pending = {}
            self.injected = False
            return {}

        return {
            "SessionStart": [HookMatcher(hooks=[start])],
            "UserPromptSubmit": [HookMatcher(hooks=[prompt])],
            "PostCompact": [HookMatcher(hooks=[compact])],
        }

    def commit(self, session_id: str | None) -> None:
        if not session_id or not self.injected or not self.prompt_seen:
            return
        state = {"schema": 1, "session_id": session_id, "hashes": self.pending}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".materials-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
