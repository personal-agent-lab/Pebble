"""会话背景去重、失效与压缩恢复；不调用真实模型。"""

import asyncio
import json

import pytest

from server.agent.context import Material
from server.agent.materials import FULL_NOTE, SessionMaterials


def hook(state, event, **data):
    async def invoke():
        result = await state.hooks()[event][0].hooks[0](data, None, {})
        return result.get("hookSpecificOutput", {}).get("additionalContext", "")

    return asyncio.run(invoke())


def saved(path, *, background, selected=()):
    state = SessionMaterials(path, None)
    state.begin()
    state.configure(background=background, selected=selected)
    hook(state, "SessionStart", source="startup")
    hook(state, "UserPromptSubmit", prompt="开始")
    state.commit("session-1")
    return state


def test_unchanged_background_survives_new_controller_without_duplicate_injection(tmp_path):
    path = tmp_path / "state.json"
    background = [Material("关于你", "住在南京"), Material("目录", {"a": "资料"})]
    saved(path, background=background)
    assert "住在南京" not in path.read_text()

    resumed = SessionMaterials(path, "session-1")
    resumed.begin()
    resumed.configure(background=background, events=[Material("当前事件", "新结果")])
    assert hook(resumed, "SessionStart", source="resume") == ""
    assert hook(resumed, "UserPromptSubmit", prompt="继续") == "## 当前事件\n新结果"
    assert hook(resumed, "UserPromptSubmit", prompt="SDK后续输入") == ""
    resumed.commit("session-1")
    assert SessionMaterials(path, "session-1").previous == resumed.pending


@pytest.mark.parametrize("updated", ["常住苏州", ""])
def test_changed_or_cleared_background_replaces_old_content(tmp_path, updated):
    path = tmp_path / "state.json"
    saved(path, background=[Material("关于你", "常住南京"), Material("约定", "先给例子")])
    state = SessionMaterials(path, "session-1")
    state.begin()
    state.configure(background=[Material("关于你", updated), Material("约定", "先给例子")])
    assert hook(state, "SessionStart", source="resume") == ""
    text = hook(state, "UserPromptSubmit", prompt="继续")
    assert "完整替代此前版本" in text
    assert f"## 关于你\n{updated}" in text
    assert "常住南京" not in text and "先给例子" not in text


def test_removed_material_and_cancelled_selection_are_explicit(tmp_path):
    path = tmp_path / "state.json"
    saved(
        path,
        background=[Material("关于你", "南京")],
        selected=[Material("手动流程", "按流程执行", key="selection:1")],
    )
    state = SessionMaterials(path, "session-1")
    state.begin()
    state.configure(background=[])
    text = hook(state, "UserPromptSubmit", prompt="换个任务")
    assert "## 关于你" in text and "## 手动流程" in text
    assert text.count("已清空或停用") == 2
    assert "按流程执行" not in text


@pytest.mark.parametrize("source", ["startup", "clear", "compact"])
def test_full_background_is_rebuilt_at_session_boundaries(tmp_path, source):
    path = tmp_path / "state.json"
    background = [Material("背景", "重要事实")]
    saved(path, background=background)
    state = SessionMaterials(path, "session-1")
    state.begin()
    state.configure(background=background, events=[Material("事件", "本次结果")])
    text = hook(state, "SessionStart", source=source)
    assert FULL_NOTE in text and "重要事实" in text and "本次结果" not in text
    assert hook(state, "UserPromptSubmit", prompt="继续") == "## 事件\n本次结果"


def test_manual_compaction_does_not_consume_task_events(tmp_path):
    path = tmp_path / "state.json"
    saved(path, background=[Material("背景", "重要事实")])
    state = SessionMaterials(path, "session-1")
    state.begin()
    state.configure(
        background=[Material("背景", "重要事实")], events=[Material("事件", "最新结果")]
    )
    assert hook(state, "UserPromptSubmit", prompt="/compact") == ""
    hook(state, "PostCompact", trigger="manual")
    text = hook(state, "UserPromptSubmit", prompt="继续")
    assert "重要事实" in text and "最新结果" in text


def test_automatic_compaction_rebuilds_without_replaying_event(tmp_path):
    state = SessionMaterials(tmp_path / "state.json", None)
    state.configure(
        background=[Material("背景", "重要事实")], events=[Material("事件", "最新结果")]
    )
    hook(state, "SessionStart", source="startup")
    hook(state, "UserPromptSubmit", prompt="继续")
    hook(state, "PostCompact", trigger="auto")
    text = hook(state, "SessionStart", source="compact")
    assert "重要事实" in text and "最新结果" not in text


def test_interruption_leaves_no_trusted_checkpoint(tmp_path):
    path = tmp_path / "state.json"
    saved(path, background=[Material("旧背景", "旧事实")])
    interrupted = SessionMaterials(path, "session-1")
    interrupted.begin()
    interrupted.configure(background=[Material("新背景", "新事实")])
    hook(interrupted, "UserPromptSubmit", prompt="继续")
    # 未收到成功结束事件，不 commit；重启后的恢复必须完整重建并撤销旧材料。
    resumed = SessionMaterials(path, "session-1")
    resumed.configure(background=[])
    assert resumed.previous == {}
    assert hook(resumed, "SessionStart", source="resume") == FULL_NOTE


def test_wrong_session_or_corrupt_checkpoint_rebuilds(tmp_path):
    path = tmp_path / "state.json"
    saved(path, background=[Material("背景", "重要事实")])
    assert SessionMaterials(path, "other-session").previous == {}
    path.write_text(json.dumps({"schema": 1, "session_id": "session-1", "hashes": {"x": 1}}))
    assert SessionMaterials(path, "session-1").previous == {}
    path.write_text("broken")
    assert SessionMaterials(path, "session-1").previous == {}


def test_same_title_materials_are_tracked_by_stable_identity(tmp_path):
    path = tmp_path / "state.json"
    saved(
        path,
        background=[],
        selected=[Material("流程", "内容A", key="a"), Material("流程", "内容B", key="b")],
    )
    state = SessionMaterials(path, "session-1")
    state.configure(
        background=[],
        selected=[Material("流程", "新版A", key="a"), Material("流程", "内容B", key="b")],
    )
    text = hook(state, "UserPromptSubmit", prompt="继续")
    assert "新版A" in text and "内容B" not in text


def test_observations_only_count_submitted_materials(tmp_path):
    path = tmp_path / "state.json"
    saved(path, background=[Material("背景", "重要事实")])
    records = []
    state = SessionMaterials(path, "session-1")
    state.configure(
        background=[Material("背景", "重要事实")],
        events=[Material("事件", "新结果")],
        observe=records.append,
    )
    hook(state, "SessionStart", source="resume")
    hook(state, "UserPromptSubmit", prompt="继续")
    assert records[-1] == [{"title": "事件", "chars": len("## 事件\n新结果")}]
