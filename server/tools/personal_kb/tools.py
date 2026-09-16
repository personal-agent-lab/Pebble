"""Agent 可见的资料库工具：保存、检索、读取（含历史版本与引用）与修改 Markdown 资料。

资料库语义全部写在这些工具的 description 里，不进基础提示，保持通用层领域中立。
工具都不产生外部副作用，因此不经过 Confirmation；每次写入都留下可读文件与 Git 提交。
读取成功时由 `kb_read_source` 提取来源记录，交给程序在时间线上展示实际读取的原文。
"""

from __future__ import annotations

from server.tools.personal_kb.service import KbStore
from server.tools.registry import SideEffect, tool

# 时间线来源卡里的原文片段上限：足够核对引用，又不让一条回答被长资料撑开。
EXCERPT_LIMIT = 600


def _saved_notice(result: dict) -> str:
    action = "资料已保存，但当前不可检索" if _stale(result) else "已保存资料"
    return f"{action}：{result['title']}。位置：{result['path']}"


def _updated_notice(result: dict) -> str:
    action = "资料已修改，但当前不可检索" if _stale(result) else "已修改资料"
    return (
        f"{action}：{result['title']}。位置：{result['path']}。"
        f"版本：{result['previous_version']} → {result['version']}"
    )


def _stale(result: dict) -> bool:
    return result.get("index_status") == "stale"


def kb_read_source(result: dict) -> dict | None:
    """读取成功后的来源记录：引用、标题与本次实际读到的原文片段。"""
    ref = result.get("ref")
    if not isinstance(ref, dict) or not ref.get("path") or not ref.get("commit"):
        return None
    body = (result.get("body") or "").strip()
    excerpt = body if len(body) <= EXCERPT_LIMIT else f"{body[:EXCERPT_LIMIT]}…"
    return {"ref": ref, "title": result.get("title"), "excerpt": excerpt}


@tool(
    name="kb_save",
    side_effect=SideEffect.LOCAL_WRITE,
    notice_renderer=_saved_notice,
)
def kb_save(
    title: str,
    body: str,
    path: str | None = None,
    tags: list[str] | None = None,
    source: dict | None = None,
    *,
    kb_store: KbStore,
    task_id: str,
) -> dict:
    """新建一份资料保存到个人资料库（Markdown 文件）：用户要留存的文档、笔记、会议纪要、
    参考内容、项目细节等需要按原文查回的内容。用户的稳定偏好与对助理的持续要求由程序
    自动写入长期记忆，不经过本工具；分不清用户想"记住偏好"还是"保存资料"时先问清楚。

    参数：title 资料标题；body 资料正文（Markdown 原文）；path 可选，资料在库内的
    相对路径（如 "课程/gse-lab1.md"），可按内容自选文件夹，省略时归入收件目录；
    tags 可选标签；source 可选来源，形如 {"kind": "user"|"mail"|"calendar"|"kb"|"task",
    "ref": 标识}。

    保存成功后向用户说明这份资料保存到了哪里（用返回的 path，这是用户可直接打开的
    资料位置），不要只说"已保存"。返回 id、path、version 与 ref。
    """
    actual_source = source or {"kind": "task", "ref": task_id}
    return kb_store.save(
        title=title,
        body=body,
        path=path,
        tags=tags,
        source=actual_source,
    )


@tool(name="kb_search", side_effect=SideEffect.READONLY)
def kb_search(
    query: str,
    source_kind: str | None = None,
    tag: str | None = None,
    max_results: int | None = None,
    *,
    kb_store: KbStore,
) -> dict:
    """按关键词检索个人资料库里已有的资料分节，返回命中的摘要、所在分节与引用（ref）。

    涉及资料中的具体事实时先用本工具检索，再对实际采用的结果调用 kb_read 读取原文；
    只有检索摘要不能作为回答依据。按内容找资料一律用本工具，不要靠列举全部资料代替检索。
    query 可以是中文词组、英文单词或编号；可选 source_kind（mail|calendar|kb|user|task）
    与 tag 限定范围；max_results 默认 10，上限 20。

    结果为空说明资料库里没有相关依据：如实告诉用户没有找到，不要凭印象作答。多份资料
    互相冲突时，把相关几份都 kb_read 出来，说明冲突及各自来源，不要自行挑一个当事实。
    """
    return kb_store.search(
        query=query, source_kind=source_kind, tag=tag, max_results=max_results
    )


@tool(name="kb_list", side_effect=SideEffect.READONLY)
def kb_list(directory: str | None = None, *, kb_store: KbStore) -> dict:
    """列出个人资料库里的 Markdown 资料，用于用户想知道库里有哪些资料、或需要按标题
    找到文件位置时。

    可选 directory 限定资料库内的目录；省略时列出整个资料库。返回每份资料的标题、
    id、path、tags 和当前 version。它只列目录和元数据，不返回正文，也不是检索：按内容
    查找与问题相关的资料用 kb_search，不要靠列清单代替检索。确定目标后再用 kb_read 读取原文。
    """
    return kb_store.list(directory=directory)


@tool(
    name="kb_read",
    side_effect=SideEffect.READONLY,
    source_extractor=kb_read_source,
)
def kb_read(
    path: str | None = None,
    id: str | None = None,
    version: str | None = None,
    ref: dict | None = None,
    *,
    kb_store: KbStore,
) -> dict:
    """读取资料库中一份资料的原文，按 path 或 id 定位，二者给其一即可；也可以按 ref 读取片段。

    返回 Markdown 原文，不做总结。需要查回答依据、核对资料具体事实，或用户想看某份资料时
    使用。指定 version（某次提交的 commit）可读取该资料的历史版本，用于对照修改前后的内容；
    省略 version 读当前版本。

    按 kb_search 返回的 ref 调用时，读取的是该引用所指版本（commit）与行号区间内的原文，
    用于引用资料中的具体事实并给出可核对的来源。ref 必须原样取自 kb_search 或 kb_read 的
    返回，不要自己拼行号或版本；引用对不上或行号越界会被拒绝。确认事实时优先按 ref 读取
    命中片段，只有确实需要通读整篇时才改用 path 或 id。
    """
    return kb_store.read(path=path, doc_id=id, version=version, ref=ref)


@tool(
    name="kb_update",
    side_effect=SideEffect.LOCAL_WRITE,
    notice_renderer=_updated_notice,
)
def kb_update(
    expected_version: str,
    path: str | None = None,
    id: str | None = None,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    source: dict | None = None,
    *,
    kb_store: KbStore,
) -> dict:
    """修改资料库中已有的一份资料，而不是新建副本；资料的稳定 id 保持不变。

    按 path 或 id 定位资料。expected_version 必填，取自最近一次 kb_read/kb_save/kb_update
    返回的 version（即该资料当前的 commit）。只传需要改的字段：title、body、tags、source；
    未传的字段保持原样。

    版本不匹配（version_conflict）说明资料自上次读取后已被改动——可能是用户直接编辑了
    文件。此时不要静默覆盖：重新 kb_read 取回最新内容与版本，确认后再改，或向用户说明。
    修改成功后向用户说明改动了哪份资料（用返回的 path），并给出新的 version。
    """
    return kb_store.update(
        expected_version=expected_version,
        path=path,
        doc_id=id,
        title=title,
        body=body,
        tags=tags,
        source=source,
    )


@tool(name="kb_history", side_effect=SideEffect.READONLY)
def kb_history(
    path: str | None = None,
    id: str | None = None,
    *,
    kb_store: KbStore,
) -> dict:
    """列出一份资料的历史版本，按 path 或 id 定位，二者给其一即可。

    返回从新到旧的版本号、修改时间和变更说明。用户要求查看修改前内容或比较版本时，
    先调用本工具取得目标 version，再调用 kb_read 并传入该 version 读取当时原文。
    """
    return kb_store.history(path=path, doc_id=id)
