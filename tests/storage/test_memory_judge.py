"""每轮专用记忆判断：提示词输入组装、近期对话窗口与判断工具契约。"""

from uuid import uuid4

import pytest

from server.agent.toolset import ToolDeps, build_tools
from server.db import init_db, session
from server.memory.judge import (
    JUDGE_MESSAGE_HEADER,
    JUDGE_RECENT_TURNS,
    build_judge_message,
    notice_texts,
    recent_text_items,
)
from server.memory.service import MemoryStore
from server.sessions import repository, timeline
from server.sessions import runs as repo
from server.sessions.service import timestamp
from server.tools.memory.tools import judge_registry
from tests.support import memory_anchor, seed_memory


def judge_tools(store):
    return {
        tool.name: tool
        for tool in build_tools(
            ToolDeps(drafts=None, tasks=None, gmail=None, memory_store=store),
            registry=judge_registry,
        )
    }


@pytest.fixture
def store(settings):
    return MemoryStore(settings.data_dir)


def add_run(conn, task_id: str, kind: str, *, status: str = "done") -> str:
    run_id = str(uuid4())
    repo.insert(conn, run_id, task_id, kind, {"message": "文本"}, None, timestamp())
    if status != "pending":
        finished = timestamp() if status in ("done", "error", "interrupted") else None
        conn.execute(
            "UPDATE agent_runs SET status = ?, started_at = ?, finished_at = ? WHERE run_id = ?",
            (status, timestamp(), finished, run_id),
        )
    return run_id


def make_task(conn) -> str:
    task_id = str(uuid4())
    repository.insert_task(conn, task_id, "任务", timestamp())
    return task_id


def seed_round(conn, task_id: str, text: str) -> str:
    run_id = add_run(conn, task_id, repo.KIND_MESSAGE)
    timeline.insert_text(conn, task_id, run_id, "user", text)
    timeline.append_assistant_text(conn, task_id, run_id, f"回复 {text}")
    return run_id


# ---------- 判断工具契约 ----------


def test_judge_registry_exposes_edit_and_ask(store):
    tools = judge_tools(store)
    assert set(tools) == {"memory_edit", "memory_ask"}
    # 前台的 memory 工具不出现在判断会话中。
    assert judge_registry.get_tool("memory") is None


def test_judge_edit_tool_writes_through_store(store):
    edit = judge_tools(store)["memory_edit"]
    assert edit([{"action": "append", "target": "user", "text": "用户对历史感兴趣"}])["changed"]
    line = memory_anchor(store, "用户对历史感兴趣")
    assert edit([{"action": "delete", "anchor": line}])["changed"] is True
    assert store.snapshot()["user"]["content"] == ""


def test_judge_ask_records_question_without_writing(store):
    tools = judge_tools(store)
    assert tools["memory_ask"]("你想改的是哪一条？") == {"question": "你想改的是哪一条？"}
    assert store.snapshot()["user"]["content"] == ""
    assert store.snapshot()["memory"]["content"] == ""


# ---------- 输入组装 ----------


def test_build_judge_message_renders_materials(store):
    seed_memory(store, "user", "已有画像")
    line = memory_anchor(store, "已有画像")
    message = build_judge_message("我对历史感兴趣", "用户：你好\n助手：你好！", store.snapshot())
    assert message.startswith(JUDGE_MESSAGE_HEADER)
    assert "## 用户刚发的消息\n我对历史感兴趣" in message
    assert "## 近期对话\n用户：你好\n助手：你好！" in message
    assert f"## 当前长期记忆：关于你（已用 4 / 上限 1375 字）\n{line}| 已有画像" in message
    assert "## 当前长期记忆：事实与约定（已用 0 / 上限 2200 字）\n（空）" in message


def test_build_judge_message_with_empty_context_and_memory(store):
    message = build_judge_message("你好", "", store.snapshot())
    assert "（无此前对话）" in message
    assert "（空）" in message


# ---------- 近期对话窗口 ----------


def test_recent_text_items_excludes_unfinished_runs(settings):
    init_db()
    with session() as conn:
        task_id = make_task(conn)
        seed_round(conn, task_id, "第一条")
        current = add_run(conn, task_id, repo.KIND_MESSAGE, status="running")
        timeline.insert_text(conn, task_id, current, "user", "刚发的消息")
        texts = [item["text"] for item in recent_text_items(conn, task_id)]
    assert "刚发的消息" not in texts
    assert texts == ["第一条", "回复 第一条"]


def test_recent_text_items_limited_to_recent_turns(settings):
    init_db()
    with session() as conn:
        task_id = make_task(conn)
        for index in range(JUDGE_RECENT_TURNS + 2):
            seed_round(conn, task_id, f"第{index}条")
        texts = [item["text"] for item in recent_text_items(conn, task_id)]
    assert "第0条" not in texts and "第1条" not in texts
    assert texts[0] == "第2条"


def test_recent_text_items_include_notices(settings):
    init_db()
    with session() as conn:
        task_id = make_task(conn)
        run_id = seed_round(conn, task_id, "第一条")
        timeline.insert_notice(conn, task_id, run_id, "想确认：你指的是哪一条？")
        items = recent_text_items(conn, task_id)
    assert items[-1] == {"kind": "notice", "role": None, "text": "想确认：你指的是哪一条？"}


# ---------- 提示文案 ----------


def edited(*applied):
    return {
        "tool": "memory_edit",
        "arguments": {"operations": []},
        "result": {"changed": True, "applied": list(applied)},
    }


def change(action, removed=(), added=(), changed=True, **extra):
    return {
        "action": action,
        "target": "user",
        "removed": list(removed),
        "added": list(added),
        "changed": changed,
        **extra,
    }


def failed(error, message):
    return {"tool": "memory_edit", "arguments": {}, "error": {"error": error, "message": message}}


def test_notice_texts_report_actual_results():
    records = [
        edited(
            change("append", added=["- 用户对历史感兴趣"]),
            change("insert", added=["- 用户偏好通俗读物"]),
            change("replace", ["- 内部会议 30 分钟"], ["- 内部会议 45 分钟"]),
            change("delete", ["- 用户偏好英文"]),
            change("move", ["- 工作日历用于内部会议"], ["- 工作日历用于内部会议"], to="memory"),
            change("append", changed=False, reason="exists"),
        ),
        {"tool": "memory_ask", "arguments": {}, "result": {"question": "要改哪一条？"}},
    ]
    assert notice_texts(records) == [
        "已更新记忆",
        "这条内容已经在记忆里。",
        "想确认：要改哪一条？",
    ]


def test_notice_texts_report_failure_only_when_last_edit_failed():
    saved = edited(change("append", added=["新偏好"]))
    assert notice_texts([failed("invalid_memory", "未通过校验"), saved]) == ["已更新记忆"]
    assert notice_texts([saved, failed("memory_full", "放不下")]) == [
        "已更新记忆",
        "记忆保存失败：放不下",
    ]
