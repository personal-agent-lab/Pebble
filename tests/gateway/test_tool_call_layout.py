"""展开的工具行面板的版面契约（前端样式表，跑在 pytest 里）。

轨迹条目在任务页展开成 `.tool-call-panel`：参数以 `dl/dt/dd` 列出、返回内容放在
`pre` 里、底部是时间与 `tool_call_id` 加复制按钮。面板高度固定（`max-height: 40vh`）
且自身滚动，因此面板子项一旦参与 flex 收缩，`pre` 会被压到比内容矮：返回内容画到
自己的盒子外面，压住底部元信息行、盖过卡片圆角下边框——返回值一长就出现这个版面缺陷。

jsdom 不做布局，量不出这种重叠，所以这条契约直接盯住 CSS 的写法。
"""

import re
from pathlib import Path

import pytest

APP_CSS = Path(__file__).resolve().parents[2] / "web/src/styles/app.css"

COMMENT = re.compile(r"/\*.*?\*/", re.S)


def rule_bodies(css: str, selector: str) -> list[str]:
    """某个选择器的规则体；同一选择器出现多次时全部取出。"""
    escaped = re.escape(selector)
    return re.findall(escaped + r"\s*\{([^}]*)\}", css, re.S)


def declarations(css: str, selector: str) -> dict[str, str]:
    """选择器的声明表：`属性 -> 值`，后写的覆盖先写的。"""
    entries: dict[str, str] = {}
    for body in rule_bodies(css, selector):
        for part in body.split(";"):
            prop, separator, value = part.partition(":")
            if separator and prop.strip():
                entries[prop.strip()] = value.strip()
    return entries


@pytest.fixture
def css() -> str:
    assert APP_CSS.is_file(), f"样式表不见了：{APP_CSS}"
    # 去掉注释再解析：注释里出现过 `min-height: 0` 这类描述，不能被当成声明。
    return COMMENT.sub("", APP_CSS.read_text(encoding="utf-8"))


def test_tool_call_panel_clips_overflow(css: str) -> None:
    panel = declarations(css, ".tool-call-panel")
    # 横向一起裁掉：不换行的长值（长 URL、长 ID）不能把面板撑宽、溢出卡片。
    assert panel["overflow"] == "hidden auto"
    assert panel["max-height"] == "40vh"


def test_tool_call_panel_children_keep_natural_height(css: str) -> None:
    # 参数表、返回内容与底部元信息行都不参与收缩：装不下时由面板滚动，
    # 而不是把 pre 压扁后让它画到元信息行上去。
    for selector in (".tool-call-fields", ".tool-call-result", ".tool-call-meta"):
        assert declarations(css, selector).get("flex") == "none", selector
    # 曾经写成 `min-height: 0`，正是它允许返回内容块被压到比 pre 矮。
    assert "min-height" not in declarations(css, ".tool-call-result")


def test_tool_call_text_wraps_inside_panel(css: str) -> None:
    for selector in (".tool-call-field dd", ".tool-call-result pre"):
        rule = declarations(css, selector)
        assert rule.get("white-space") == "pre-wrap", selector
        assert rule.get("overflow-wrap") == "anywhere", selector
    # 参数值所在的网格列必须能收到 0 宽，否则长值会把整列撑开。
    columns = declarations(css, ".tool-call-field")["grid-template-columns"]
    assert columns == "minmax(80px, max-content) minmax(0, 1fr)"
