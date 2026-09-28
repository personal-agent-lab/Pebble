"""种子数据目录构建：文件形态与真实实例一致。

- `memory/`：两个 Markdown 直接写入（实例初始状态，不属于任何用户动作）。
- `kb/`：只落正文（可带一级标题），默认不写 YAML 头——文档头由 intake 在首次
  资料库操作时补全（`kb.md` §4.1，用户直改磁盘是一等公民入口）。语料本身放在
  `evaluation/seeds/`，与真实 `kb/` 同构，可读可 diff。
- 例外：语料自带的 `summary` 会被保留，用来构造"经助手保存、有摘要"与
  "用户编辑、无摘要"混杂的真实状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KbDoc:
    title: str
    body: str
    directory: str | None = None
    summary: str | None = None


class SeedBuilder:
    """在实例数据目录里铺出受控初始状态；实例启动前调用。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def memory(self, user_md: str, memory_md: str) -> None:
        memory_dir = self.data_dir / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "USER.md").write_text(user_md, encoding="utf-8")
        (memory_dir / "MEMORY.md").write_text(memory_md, encoding="utf-8")

    def kb_docs(self, docs: list[KbDoc]) -> None:
        for doc in docs:
            target_dir = self.data_dir / "kb"
            if doc.directory:
                target_dir = target_dir / doc.directory
            target_dir.mkdir(parents=True, exist_ok=True)
            body = doc.body.strip() + "\n"
            # 有摘要的资料带一个只含摘要的 YAML 头，其余交给 intake 补全（`kb.md` §4.1）。
            head = f"---\nsummary: {doc.summary.strip()}\n---\n\n" if doc.summary else ""
            (target_dir / f"{doc.title}.md").write_text(head + body, encoding="utf-8")
