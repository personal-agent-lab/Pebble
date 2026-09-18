"""按行锚点编辑 Markdown 文本：给模型看的带锚点视图，以及按锚点执行的一组整行修改。

锚点由“所在部分的名称 + 这一行的文字”算出，模型只需写锚点与新内容，不必照抄原文；
一行被改过，它的锚点随之失效，修改被拒绝，因此锚点本身就是逐行的冲突检查。
一次调用可以包含多处修改，锚点都指调用前的内容，整体生效或整体失败。

这里只处理文本结构，不含任何领域语义：各领域决定有哪些部分、要不要查重、
失败时交回什么内容，并把 `LineEditError` 转成自己的错误。
"""

from __future__ import annotations

import base64
import hashlib
import re

ANCHOR_PREFIX = re.compile(r"^\s*[a-z0-9]{4}\|")
LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
EMPTY_VIEW = "（空）"
# 每种动作的必填与可选字段。
ACTIONS = {
    "append": (("target", "text"), ()),
    "insert": (("after", "text"), ()),
    "replace": (("anchor", "text"), ("end_anchor",)),
    "delete": (("anchor",), ("end_anchor",)),
    "move": (("anchor", "to"), ("end_anchor",)),
}
ANCHOR_FIELDS = ("anchor", "end_anchor", "after")


class LineEditError(Exception):
    """修改的字段或定位条件不合法；`errors` 为字段与原因列表，整次修改未生效。"""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(str(errors))


def anchors(contents: dict[str, str]) -> dict[str, list[str | None]]:
    """每个部分逐行的锚点（空行为 None）：由部分名称与行文字算出，所有部分合起来唯一。

    重复行或哈希碰撞按出现顺序加盐顺延，结果只取决于内容本身。
    """
    used: set[str] = set()
    result: dict[str, list[str | None]] = {}
    for name, content in contents.items():
        column: list[str | None] = []
        for line in _lines(content):
            if not line.strip():
                column.append(None)
                continue
            salt = 0
            while (anchor := _anchor(name, line.rstrip(), salt)) in used:
                salt += 1
            used.add(anchor)
            column.append(anchor)
        result[name] = column
    return result


def render(contents: dict[str, str]) -> dict[str, str]:
    """每个部分给模型看的文本：有文字的行写成“锚点| 原文”，空行原样保留。"""
    column = anchors(contents)
    return {name: _render(_lines(content), column[name]) for name, content in contents.items()}


def parse_operations(operations: object, names: tuple[str, ...]) -> list[dict]:
    """检查动作与字段：字段不全或新文字带锚点前缀时整次拒绝，不替模型修正。

    只有一个部分时 `append` 可省略 target，也没有 `move`。
    """
    if not isinstance(operations, list) or not operations:
        raise LineEditError([{"field": "operations", "message": "至少需要一处修改"}])
    single = len(names) == 1
    actions = [action for action in ACTIONS if not (single and action == "move")]
    errors: list[dict[str, str]] = []
    parsed = []
    for index, item in enumerate(operations, start=1):
        field = f"operations[{index}]"
        if not isinstance(item, dict):
            errors.append({"field": field, "message": f"第 {index} 处修改格式不正确"})
            continue
        action = item.get("action")
        if action not in actions:
            errors.append(
                {
                    "field": f"{field}.action",
                    "message": f"第 {index} 处：action 必须是 {'、'.join(actions)} 之一",
                }
            )
            continue
        required, optional = ACTIONS[action]
        op: dict = {"index": index, "action": action}
        if single and action == "append" and item.get("target") is None:
            op["target"] = names[0]
            required = tuple(key for key in required if key != "target")
        for key in (*required, *optional):
            value = item.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                if key in required:
                    errors.append(
                        {
                            "field": f"{field}.{key}",
                            "message": f"第 {index} 处：{action} 需要 {key}",
                        }
                    )
                continue
            if not isinstance(value, str):
                errors.append(
                    {"field": f"{field}.{key}", "message": f"第 {index} 处：{key} 必须是字符串"}
                )
                continue
            if key in ANCHOR_FIELDS:
                value = value.strip().rstrip("|").strip().lower()
            elif key in ("target", "to"):
                value = value.strip()
                if value not in names:
                    errors.append(
                        {
                            "field": f"{field}.{key}",
                            "message": f"第 {index} 处：{key} 必须是 {' 或 '.join(names)}",
                        }
                    )
                    continue
            else:
                value = _text_lines(value)
                if any(ANCHOR_PREFIX.match(line) for line in value):
                    errors.append(
                        _op_error(index, "text", "text 里有锚点前缀，锚点不是内容，只写原文")
                    )
                    continue
            op[key] = value
        parsed.append(op)
    if errors:
        raise LineEditError(errors)
    return parsed


def apply_operations(
    contents: dict[str, str], operations: list[dict], *, dedupe: bool = False
) -> tuple[dict[str, str], list[dict]]:
    """在调用前的内容上定位全部修改，再逐部分生成新内容；任何定位错误都整次拒绝。

    `dedupe` 为真时，新增的行若在修改后的同一部分里已有（忽略列表标记与首尾空白），
    就不再写入，记录里标 `reason: exists`。返回新内容（未做空行整理）与按调用顺序的
    逐项记录：`removed`/`added` 为实际删除与写入的整行。
    """
    names = tuple(contents)
    lines = {name: _lines(contents[name]) for name in names}
    column = anchors(contents)
    where = {
        anchor: (name, row)
        for name in names
        for row, anchor in enumerate(column[name])
        if anchor is not None
    }
    errors: list[dict[str, str]] = []

    def locate(op: dict, key: str) -> tuple[str, int] | None:
        found = where.get(op[key])
        if found is None:
            errors.append(
                _op_error(
                    op["index"],
                    key,
                    f"锚点 {op[key]} 不存在（那一行可能已被改动），请按最新内容重新定位",
                )
            )
        return found

    # 第一遍：替换、删除、移动认领行范围。
    claimed: dict[tuple[str, int], dict] = {}
    ranges: dict[int, tuple[str, int, int]] = {}
    for op in operations:
        if op["action"] not in ("replace", "delete", "move"):
            continue
        start = locate(op, "anchor")
        end = locate(op, "end_anchor") if "end_anchor" in op else start
        if start is None or end is None:
            continue
        name, first = start
        if end[0] != name or end[1] < first:
            message = (
                "end_anchor 不能在 anchor 之前"
                if len(names) == 1
                else "end_anchor 必须与 anchor 在同一分区且不在它之前"
            )
            errors.append(_op_error(op["index"], "end_anchor", message))
            continue
        if op["action"] == "move" and op["to"] == name:
            errors.append(
                {
                    "field": f"operations[{op['index']}].to",
                    "message": f"第 {op['index']} 处：to 必须是另一个分区",
                }
            )
            continue
        overlap = [
            claimed[(name, row)]["index"]
            for row in range(first, end[1] + 1)
            if (name, row) in claimed
        ]
        if overlap:
            errors.append(
                {
                    "field": f"operations[{op['index']}].anchor",
                    "message": f"第 {op['index']} 处与第 {overlap[0]} 处修改了同一行",
                }
            )
            continue
        for row in range(first, end[1] + 1):
            claimed[(name, row)] = op
        ranges[op["index"]] = (name, first, end[1])

    # 第二遍：插入挂到锚点行下面。
    inserts: dict[tuple[str, int], list[dict]] = {}
    for op in operations:
        if op["action"] != "insert" or (found := locate(op, "after")) is None:
            continue
        owner = claimed.get(found)
        if owner is not None and owner["action"] != "replace":
            errors.append(
                _op_error(
                    op["index"], "after", f"插入位置那一行被第 {owner['index']} 处删除或移走了"
                )
            )
            continue
        inserts.setdefault(found, []).append(op)

    if errors:
        raise LineEditError(errors)

    # 查重基准：每个部分调用后仍保留的行，加上替换写入的行。
    present: dict[str, set[str]] = {name: set() for name in names}
    for name in names:
        for row, line in enumerate(lines[name]):
            if line.strip() and (name, row) not in claimed:
                present[name].add(_bare(line))
    records: dict[int, dict] = {}
    replacements: dict[int, list[str]] = {}
    for op in operations:
        if op["action"] != "replace":
            continue
        name, first, last = ranges[op["index"]]
        original = [line for line in lines[name][first : last + 1] if line.strip()]
        text = list(op["text"])
        marker = LIST_MARKER.match(original[0])
        if first == last and len(text) == 1 and marker and not LIST_MARKER.match(text[0]):
            text = [marker.group(0) + text[0].strip()]
        replacements[op["index"]] = text
        present[name].update(_bare(line) for line in text if line.strip())
        same = text == original
        records[op["index"]] = {
            "action": "replace",
            "target": name,
            "removed": original,
            "added": text,
            "changed": not same,
            **({"reason": "same"} if same else {}),
        }

    # 按列表顺序决定新增内容是否写入（查重时同一次调用里后出现的重复只写第一处）。
    accepted: dict[int, list[str]] = {}
    appends: dict[str, list[list[str]]] = {name: [] for name in names}
    for op in operations:
        action = op["action"]
        if action == "replace":
            continue
        if action == "delete":
            name, first, last = ranges[op["index"]]
            removed = [line for line in lines[name][first : last + 1] if line.strip()]
            records[op["index"]] = {
                "action": "delete",
                "target": name,
                "removed": removed,
                "added": [],
                "changed": True,
            }
            continue
        if action == "move":
            source, first, last = ranges[op["index"]]
            text = [line for line in lines[source][first : last + 1] if line.strip()]
            destination = op["to"]
        else:
            text = op["text"]
            destination = where[op["after"]][0] if action == "insert" else op["target"]
        new = [_bare(line) for line in text if line.strip()]
        exists = dedupe and all(line in present[destination] for line in new)
        if not exists:
            present[destination].update(new)
            accepted[op["index"]] = text
            if action != "insert":
                appends[destination].append(text)
        if action == "move":
            records[op["index"]] = {
                "action": "move",
                "target": source,
                "from": source,
                "to": destination,
                "removed": text,
                "added": [] if exists else text,
                "changed": True,
            }
        else:
            records[op["index"]] = {
                "action": action,
                "target": destination,
                "removed": [],
                "added": [] if exists else text,
                "changed": not exists,
                **({"reason": "exists"} if exists else {}),
            }

    updated = {}
    for name in names:
        out: list[str] = []
        row = 0
        while row < len(lines[name]):
            owner = claimed.get((name, row))
            if owner is None:
                out.append(lines[name][row])
                out.extend(_inserted(inserts.get((name, row), []), accepted))
                row += 1
                continue
            _, first, last = ranges[owner["index"]]
            if owner["action"] == "replace":
                out.extend(replacements[owner["index"]])
                for inner in range(first, last + 1):
                    out.extend(_inserted(inserts.get((name, inner), []), accepted))
            row = last + 1
        for text in appends[name]:
            while out and not out[-1].strip():
                out.pop()
            if (
                out
                and LIST_MARKER.match(out[-1])
                and all(LIST_MARKER.match(line) for line in text if line.strip())
            ):
                out.extend(text)
            elif out:
                out.extend(["", *text])
            else:
                out.extend(text)
        updated[name] = "\n".join(out)
    return updated, [records[op["index"]] for op in operations]


def _anchor(name: str, line: str, salt: int) -> str:
    digest = hashlib.blake2s(f"{name}\n{line}\n{salt}".encode()).digest()
    return base64.b32encode(digest).decode().lower()[:4]


def _lines(content: str) -> list[str]:
    return content.split("\n") if content else []


def _render(lines: list[str], column: list[str | None]) -> str:
    if not lines:
        return EMPTY_VIEW
    return "\n".join(
        line if anchor is None else f"{anchor}| {line}"
        for line, anchor in zip(lines, column, strict=True)
    )


def _text_lines(value: str) -> list[str]:
    """写入的整行：统一换行、去掉行尾空白与首尾空行，保留缩进（嵌套列表与代码块需要）。"""
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _bare(line: str) -> str:
    return LIST_MARKER.sub("", line).strip()


def _op_error(index: int, key: str, message: str) -> dict[str, str]:
    return {"field": f"operations[{index}].{key}", "message": f"第 {index} 处：{message}"}


def _inserted(ops: list[dict], accepted: dict[int, list[str]]) -> list[str]:
    return [line for op in ops for line in accepted.get(op["index"], [])]
