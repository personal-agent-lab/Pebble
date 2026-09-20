"""资料目录：每轮常驻上下文里的资料库指针，由程序从资料文件现算，不存成资料。

记忆是“不查就必须生效”的内容，全文常驻；资料是“知道有它、需要时再查”的内容，
常驻的只是这份目录——每份资料的标题与一句话说明，按目录分组。目录有容量上限：
资料多了按最近更新优先列出，列不下的合并成“另有 N 份”，仍可靠检索找到。
"""

from __future__ import annotations

from dataclasses import dataclass

CATALOG_TITLE = "资料目录"
# 目录的字符上限：和两份长期记忆（1375 + 2200）同一量级，每轮带着不显著增加上下文。
CATALOG_LIMIT = 1500
# 单条说明的截断长度：一句话足够判断要不要去读，长说明不挤占其他资料的位置。
SUMMARY_LIMIT = 60
ROOT_LABEL = "（根目录）"


@dataclass(frozen=True)
class CatalogEntry:
    path: str
    title: str
    summary: str | None
    updated_at: str


def render_catalog(entries: list[CatalogEntry], limit: int = CATALOG_LIMIT) -> str:
    """把资料按目录分组渲染成目录文本；超出上限时按最近更新优先保留条目。

    目录按其中最近一次更新排序，目录内的资料同样最近更新在前。先保证每个目录都有
    分组行（放不下的目录合并进末尾的汇总），再按全局更新时间逐条填入资料，
    每个目录没列出的资料用“另有 N 份”说明。
    """
    if not entries:
        return ""
    ordered = sorted(entries, key=lambda entry: (entry.updated_at, entry.path), reverse=True)
    groups: dict[str, list[CatalogEntry]] = {}
    for entry in ordered:
        groups.setdefault(_directory(entry.path), []).append(entry)
    names = list(groups)

    shown_groups = len(names)
    while shown_groups > 0:
        if len(_render(groups, names[:shown_groups], set(), len(ordered))) <= limit:
            break
        shown_groups -= 1
    visible = names[:shown_groups]
    selected: set[str] = set()
    for entry in ordered:
        if _directory(entry.path) not in visible:
            continue
        trial = selected | {entry.path}
        if len(_render(groups, visible, trial, len(ordered))) <= limit:
            selected = trial
    return _render(groups, visible, selected, len(ordered))


def _render(
    groups: dict[str, list[CatalogEntry]], visible: list[str], selected: set[str], total: int
) -> str:
    lines = [f"资料库共 {total} 份资料；需要细节时用 kb_search 检索，或用 kb_read 读取原文。"]
    for name in visible:
        documents = groups[name]
        lines.append(f"- {name}（{len(documents)} 份）")
        listed = [document for document in documents if document.path in selected]
        for document in listed:
            lines.append(f"  - {_line(document)}")
        rest = len(documents) - len(listed)
        if rest and listed:
            lines.append(f"  - 另有 {rest} 份")
    hidden = [name for name in groups if name not in visible]
    if hidden:
        count = sum(len(groups[name]) for name in hidden)
        lines.append(f"- 另有 {len(hidden)} 个目录共 {count} 份资料未列出")
    return "\n".join(lines)


def _line(entry: CatalogEntry) -> str:
    summary = (entry.summary or "").strip().replace("\n", " ")
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[:SUMMARY_LIMIT] + "…"
    return f"{entry.title}：{summary}" if summary else entry.title


def _directory(path: str) -> str:
    relative = path[3:] if path.startswith("kb/") else path
    parent, _, _ = relative.rpartition("/")
    return f"{parent}/" if parent else ROOT_LABEL
