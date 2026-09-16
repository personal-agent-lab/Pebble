"""Agent 可见的资料库工具：保存、读取（含历史版本）与修改 Markdown 资料。

资料库语义全部写在这些工具的 description 里，不进基础提示，保持通用层领域中立。
工具都不产生外部副作用，因此不经过 Confirmation；每次写入都留下可读文件与 Git 提交。
"""

from __future__ import annotations

from server.tools.personal_kb.service import KbStore
from server.tools.registry import SideEffect, tool


def _saved_notice(result: dict) -> str:
    return f"已保存资料：{result['title']}。位置：{result['path']}"


def _updated_notice(result: dict) -> str:
    return (
        f"已修改资料：{result['title']}。位置：{result['path']}。"
        f"版本：{result['previous_version']} → {result['version']}"
    )


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
    """新建一份资料保存到个人资料库（Markdown 文件），用于用户想留存的具体资料。

    什么时候用：用户要保存一份资料、文档、会议纪要、笔记、参考内容、项目细节等
    值得长期留存并按原文查回的内容。这与"记住偏好"不同——用户的稳定偏好、背景、
    对助理的持续要求由程序自动写入长期记忆，你不用、也不要把整段资料塞进记忆。
    不确定用户是想让你长期记住某个偏好、还是想保存一份资料时，先问清楚再操作。

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


@tool(name="kb_list", side_effect=SideEffect.READONLY)
def kb_list(directory: str | None = None, *, kb_store: KbStore) -> dict:
    """列出个人资料库里的 Markdown 资料，用于尚不知道资料 path 或 id 时发现资料。

    可选 directory 限定资料库内的目录；省略时列出整个资料库。返回每份资料的标题、
    id、path、tags 和当前 version。它只列目录和元数据，不搜索或返回正文；确定目标后再用
    kb_read 读取原文。用户只给出标题或大致文件位置时，先用本工具定位。
    """
    return kb_store.list(directory=directory)


@tool(name="kb_read", side_effect=SideEffect.READONLY)
def kb_read(
    path: str | None = None,
    id: str | None = None,
    version: str | None = None,
    *,
    kb_store: KbStore,
) -> dict:
    """读取资料库中一份资料的原文，按 path 或 id 定位，二者给其一即可。

    返回 Markdown 原文，不做总结。需要查回答依据、核对资料具体事实，或用户想看
    某份资料时使用。指定 version（某次提交的 commit）可读取该资料的历史版本，用于
    对照修改前后的内容；省略 version 读当前版本。返回 title、tags、source、body 与 ref。
    """
    return kb_store.read(path=path, doc_id=id, version=version)


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
