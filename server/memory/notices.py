"""记忆会话的共用部分：给模型的当前记忆材料，以及按真实工具结果生成的用户提示。

提示只依据记录到的工具调用与结果生成，模型自述不作为事实来源。每轮判断的每项结果都
提示；后台回顾只提示修改与删除（整理动了用户已有的内容），新增不打扰用户。
"""

from __future__ import annotations

from server.agent.context import Material

EMPTY_MEMORY = "（空）"
MEMORY_TITLES = (("user", "关于你"), ("memory", "事实与约定"))

# 判断失败才提示用户的错误；invalid_memory 是模型可自行修正的参数问题，不打扰用户。
NOTICE_FAILURE_ERRORS = {"memory_full", "memory_store_unavailable", "unexpected"}
REVIEW_PREFIX = "整理记忆："


def memory_materials(snapshot: dict) -> tuple[Material, ...]:
    """当前长期记忆的两块材料，标题附带容量，模型据此判断是否需要先整理。"""
    materials = []
    for target, label in MEMORY_TITLES:
        current = snapshot[target]
        usage = current["usage"]
        title = f"当前长期记忆：{label}（已用 {usage['chars']} / 上限 {usage['limit']} 字）"
        materials.append(Material(title, current["content"] or EMPTY_MEMORY))
    return tuple(materials)


def notice_texts(records: list[dict]) -> list[str]:
    """每轮判断的实际工具调用与结果映射为用户可见的提示，按调用顺序；多处编辑逐处提示。"""
    notices = []
    for index, record in enumerate(records):
        if "error" in record:
            error = record["error"]
            if error["error"] not in NOTICE_FAILURE_ERRORS:
                continue
            # 容量不足后模型先整理再保存成功的，不再提示那次失败。
            if error["error"] == "memory_full" and _saved_later(record, records[index + 1 :]):
                continue
            notices.append(f"记忆保存失败：{error['message']}")
            continue
        if record["tool"] == "memory_ask":
            notices.append(f"想确认：{record['result']['question']}")
            continue
        for edit in record["result"]["applied"]:
            notices.append(_change_text(edit) if edit["changed"] else "这条内容已经在记忆里。")
    return notices


def review_notice_texts(records: list[dict]) -> list[str]:
    """后台回顾的提示：只列出实际生效的修改与删除。"""
    return [
        REVIEW_PREFIX + _change_text(edit, review=True)
        for record in records
        for edit in record.get("result", {}).get("applied", [])
        if edit["changed"] and edit["old_text"]
    ]


def _change_text(edit: dict, *, review: bool = False) -> str:
    old, new = edit["old_text"], edit["new_text"]
    if not old:
        return f"已记住：{new}"
    if new:
        return f"已修改：{old} → {new}"
    return f"已删除：{old}" if review else f"已删除这条记忆：{old}。原对话仍保留。"


def _requested_texts(arguments: dict) -> set[str]:
    """一次失败调用想写入的新文字；operations 格式不对时按没有处理。"""
    operations = arguments.get("operations")
    edits = operations if isinstance(operations, list) else [arguments]
    return {
        (edit.get("new_text") or "").strip()
        for edit in edits
        if isinstance(edit, dict) and isinstance(edit.get("new_text"), str)
    } - {""}


def _saved_later(failed: dict, later: list[dict]) -> bool:
    wanted = _requested_texts(failed["arguments"])
    saved = {
        edit["new_text"]
        for record in later
        for edit in record.get("result", {}).get("applied", [])
        if edit["changed"]
    }
    return bool(wanted) and wanted <= saved
