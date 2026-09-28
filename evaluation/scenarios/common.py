"""共享设定：测试人格「林然」的种子（docs/evaluation.md §4.4）。

语料原文是 `evaluation/seeds/` 下的文件，与真实 `kb/`、`memory/` 同构：谁都能直接打开改，
不必钻进 Python 字符串。本模块只负责读入并校验形态，不生成内容——同一种子必须可复现
（评测规格 §8），所以语料是常量，不调模型生成、不读真实实例数据。

形态校验在铺种子时执行，语料写错就地报错（附修改指引），不带着坏语料去开实例。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from ..harness.config import seeds_root
from ..harness.seeds import KbDoc, SeedBuilder

# 记忆两份：只写"已经说好的事"，具体事实（邮箱、地址、预约电话）留在资料库里待检索。
USER_MD = """\
# 关于你

- 林然，产品经理，在云枢科技增长产品组，常驻上海；工作日被站会、评审填满。
- 中文交流，语气直接但讲礼貌；对外邮件保持专业措辞，不用感叹号。
- 老家苏州，妈妈还在那边；每年体检，妈妈生日 11 月 8 日要提前订蛋糕。
- 健身跟私教 Kevin；口腔固定在明亮口腔（静安寺店）；弟弟阿凯在东京工作。
"""

MEMORY_MD = """\
# 事实与约定

## 沟通

- 邮件语气保持客气、专业；给老周的汇报只讲结论和数字。
- 不确认的事不要替我说成已经办成。

## 习惯

- 会议纪要要区分"已确定"和"待决"，待决事项不写负责人。
- 报销单笔低于 200 元不用留凭证，超过的要拍照存档。
- 周末尽量不排工作日程；出差回来那天也不排。
- 出行安排优先高铁，两小时以内的路程不坐飞机。

## 已知的坑

- 资料库里有过期的旧文件（旧保险单据、旧房租合同、改了一半的备份），引用前先确认时间。
"""

# 语料原文的必备形态（与 kb.md §1、§2 的文档约束一致）。
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_SUMMARY_RE = re.compile(r"^summary:\s*(.+)$", re.MULTILINE)
_MAX_STEM = 60

_HINT = (
    "种子语料形态不对（起始状态不可复现）。请检查 evaluation/seeds/："
    "kb/ 下每份资料要有 `# 标题`、文件名去扩展名后 ≤60 字符且在目录内唯一；"
    "两份记忆要用 USER.md / MEMORY.md 的文件名。"
)


def _document(path: Path, relative: Path) -> KbDoc:
    """读一份语料：标题取文件名，目录取自相对路径，`summary` 头按原样保留。"""
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    meta_block = ""
    if match := _FRONT_RE.match(normalized):
        meta_block, normalized = match.group(1), match.group(2)
    summary = _SUMMARY_RE.search(meta_block) if meta_block else None
    directory = str(relative.parent)
    return KbDoc(
        title=relative.stem,
        body=normalized.lstrip("\n"),
        directory=None if directory == "." else directory,
        summary=summary.group(1).strip() if summary else None,
    )


@lru_cache(maxsize=1)
def seed_corpus() -> tuple[tuple[KbDoc, ...], str, str]:
    """语料原文：资料集 + 两份记忆。读一次缓存，实例之间共享的是同一份常量。"""
    root = seeds_root()
    memory_dir = root / "memory"
    for name in ("USER.md", "MEMORY.md"):
        if not (memory_dir / name).is_file():
            raise RuntimeError(f"{_HINT}（缺少 seeds/memory/{name}）")

    kb_root = root / "kb"
    paths = sorted(kb_root.rglob("*.md"))
    if not paths:
        raise RuntimeError(f"{_HINT}（seeds/kb/ 下没有任何资料）")

    documents: list[KbDoc] = []
    seen: set[str] = set()
    for path in paths:
        relative = path.relative_to(kb_root)
        key = f"{relative.parent}/{relative.stem}"
        if key in seen:  # 同一目录内标题（文件名）必须唯一，否则落盘会互相覆盖
            raise RuntimeError(f"{_HINT}（{relative.parent} 下有同名资料 {relative.stem}）")
        if len(relative.stem) > _MAX_STEM:
            raise RuntimeError(f"{_HINT}（{relative} 标题超过 {_MAX_STEM} 字符）")
        heading = _TITLE_RE.search(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        if heading is None:
            raise RuntimeError(f"{_HINT}（{relative} 缺少 `# 标题` 一级标题）")
        # 语料里正文标题与文件名必须一致：落盘文件名取自 `KbDoc.title`，而正文一级标题
        # 是人和检索看到的那一份，两者对不上就是语料自身的坑（改文件名时最容易漏）。
        if heading.group(1).strip() != relative.stem:
            raise RuntimeError(
                f"{_HINT}（{relative} 正文标题 `{heading.group(1).strip()}` 与文件名不一致）"
            )
        seen.add(key)
        documents.append(_document(path, relative))

    return (
        tuple(documents),
        (memory_dir / "USER.md").read_text(encoding="utf-8"),
        (memory_dir / "MEMORY.md").read_text(encoding="utf-8"),
    )


def base_seed(builder: SeedBuilder) -> None:
    """林然的初始状态：两份记忆 + 全量资料语料。"""
    documents, user_md, memory_md = seed_corpus()
    builder.memory(user_md, memory_md)
    builder.kb_docs(list(documents))
