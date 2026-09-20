"""资料的按行锚点修改：只动锚点指到的行，锚点失效时整次拒绝并交回最新正文。"""

import subprocess

import pytest

from server.errors import KbValidationError, VersionConflictError, error_details
from server.tools.personal_kb.service import KbStore
from server.tools.registry import default_registry

BODY = (
    "## 安排\n"
    "\n"
    "截止日期：10 月 8 日\n"
    "\n"
    "- 第一步：读论文\n"
    "  - 精读第三节\n"
    "- 第二步：写报告\n"
    "\n"
    "## 备注\n"
    "\n"
    "无"
)


def read_tool(store, **args):
    return default_registry.get_tool("kb_read").func(kb_store=store, **args)


def update_tool(store, **args):
    return default_registry.get_tool("kb_update").func(kb_store=store, **args)


def anchor_of(view: str, line: str) -> str:
    for row in view.split("\n"):
        if row.endswith("| " + line):
            return row.split("|")[0]
    raise AssertionError(line)


def test_operations_change_only_anchored_lines(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="实验安排", body=BODY)
    view = read_tool(store, path=saved["path"])["anchored_body"]

    updated = update_tool(
        store,
        path=saved["path"],
        expected_version=saved["version"],
        operations=[
            {
                "action": "replace",
                "anchor": anchor_of(view, "截止日期：10 月 8 日"),
                "text": "截止日期：10 月 15 日（已延期）",
            },
            {
                "action": "insert",
                "after": anchor_of(view, "  - 精读第三节"),
                "text": "  - 复现实验",
            },
            {"action": "delete", "anchor": anchor_of(view, "无")},
            {"action": "append", "text": "- 助教：王老师"},
        ],
    )

    body = store.read(path=updated["path"])["body"]
    assert body == BODY.replace("10 月 8 日", "10 月 15 日（已延期）").replace(
        "  - 精读第三节\n", "  - 精读第三节\n  - 复现实验\n"
    ).replace("\n\n无", "\n\n- 助教：王老师")
    assert [item["action"] for item in updated["applied"]] == [
        "replace",
        "insert",
        "delete",
        "append",
    ]
    assert updated["applied"][0]["removed"] == ["截止日期：10 月 8 日"]
    assert "target" not in updated["applied"][0]
    message = subprocess.run(
        ["git", "-C", str(settings.data_dir), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert message == "[Kb] Edit (4 changes) 实验安排"


def test_stale_anchor_is_rejected_with_current_anchored_body(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="实验安排", body=BODY)
    view = read_tool(store, path=saved["path"])["anchored_body"]
    stale = anchor_of(view, "截止日期：10 月 8 日")
    changed = store.update(
        path=saved["path"],
        expected_version=saved["version"],
        body=BODY.replace("10 月 8 日", "10 月 9 日"),
    )
    file = settings.data_dir / changed["path"]
    before = file.read_bytes()

    with pytest.raises(KbValidationError) as raised:
        store.update(
            path=changed["path"],
            expected_version=changed["version"],
            operations=[{"action": "delete", "anchor": stale}],
        )

    document = raised.value.document
    assert document["version"] == changed["version"]
    assert "| 截止日期：10 月 9 日" in document["body"]
    assert error_details(raised.value)["document"] == document
    assert file.read_bytes() == before


def test_operations_still_require_current_version(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="实验安排", body=BODY)
    view = read_tool(store, path=saved["path"])["anchored_body"]
    store.update(path=saved["path"], expected_version=saved["version"], summary="实验安排")

    with pytest.raises(VersionConflictError):
        store.update(
            path=saved["path"],
            expected_version=saved["version"],
            operations=[{"action": "delete", "anchor": anchor_of(view, "无")}],
        )


def test_invalid_operations_are_rejected_without_writing(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="实验安排", body=BODY)
    view = read_tool(store, path=saved["path"])["anchored_body"]
    only = anchor_of(view, "无")
    file = settings.data_dir / saved["path"]
    before = file.read_bytes()

    for kwargs in (
        {"body": "新正文", "operations": [{"action": "append", "text": "x"}]},
        {"operations": []},
        {"operations": [{"action": "move", "anchor": only, "to": "body"}]},
        {"operations": [{"action": "append", "text": f"{only}| 带锚点"}]},
        {"operations": [{"action": "replace", "anchor": only}]},
    ):
        with pytest.raises(KbValidationError):
            store.update(path=saved["path"], expected_version=saved["version"], **kwargs)
    # 删掉所有行等于清空正文，同样拒绝。
    first = anchor_of(view, "## 安排")
    with pytest.raises(KbValidationError, match="正文不能为空"):
        store.update(
            path=saved["path"],
            expected_version=saved["version"],
            operations=[{"action": "delete", "anchor": first, "end_anchor": only}],
        )
    assert file.read_bytes() == before


def test_read_tool_anchors_only_current_full_reads(settings):
    store = KbStore(settings.data_dir)
    saved = store.save(title="实验安排", body=BODY)

    current = read_tool(store, path=saved["path"])
    assert "body" not in current
    assert current["anchored_body"].split("\n")[1] == ""
    assert all("| " in row for row in current["anchored_body"].split("\n") if row.strip())

    historical = read_tool(store, path=saved["path"], version=saved["version"])
    assert historical["body"] == BODY and "anchored_body" not in historical
    fragment = read_tool(store, ref=saved["ref"])
    assert "anchored_body" not in fragment


def test_update_tool_declares_operations_schema():
    schema = default_registry.get_tool("kb_update").parameters_schema["properties"]
    actions = schema["operations"]["items"]["properties"]["action"]["enum"]
    assert actions == ["append", "insert", "replace", "delete"]
