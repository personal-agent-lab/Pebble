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


def test_judge_registry_exposes_four_judgment_tools(store):
    tools = judge_tools(store)
    assert set(tools) == {"memory_add", "memory_replace", "memory_remove", "memory_ask"}
    # 前台的 memory 工具不出现在判断会话中。
    assert judge_registry.get_tool("memory") is None


def test_judge_tools_apply_through_store(store):
    tools = judge_tools(store)
    assert tools["memory_add"]("user", "用户对历史感兴趣")["changed"] is True
    assert (
        tools["memory_replace"]("user", "用户偏好通俗的历史读物", "对历史感兴趣")["changed"]
        is True
    )
    assert "通俗" in store.snapshot()["user"]["content"]
    assert tools["memory_remove"]("user", "历史读物")["changed"] is True
    assert store.snapshot()["user"]["entries"] == []


def test_judge_ask_records_question_without_writing(store):
    tools = judge_tools(store)
    assert tools["memory_ask"]("你想改的是哪一条？") == {"question": "你想改的是哪一条？"}
    assert store.snapshot()["user"]["entries"] == []
    assert store.snapshot()["memory"]["entries"] == []


# ---------- 输入组装 ----------


def test_build_judge_message_renders_materials(store):
    store.apply("add", "user", "已有画像")
    message = build_judge_message("我对历史感兴趣", "用户：你好\n助手：你好！", store.snapshot())
    assert message.startswith(JUDGE_MESSAGE_HEADER)
    assert "## 用户刚发的消息\n我对历史感兴趣" in message
    assert "## 近期对话\n用户：你好\n助手：你好！" in message
    assert "## 当前长期记忆：关于你\n已有画像" in message
    assert "## 当前长期记忆：事实与约定\n（空）" in message


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


def added(content, changed=True):
    return {
        "tool": "memory_add",
        "arguments": {"target": "user", "content": content},
        "result": {"action": "add", "changed": changed, "old": None},
    }


def test_notice_texts_report_actual_results():
    records = [
        added("用户对历史感兴趣"),
        {
            "tool": "memory_replace",
            "arguments": {"target": "user", "content": "用户偏好通俗历史读物", "old_text": "历史"},
            "result": {"action": "replace", "changed": True, "old": "用户对历史感兴趣"},
        },
        {
            "tool": "memory_remove",
            "arguments": {"target": "user", "old_text": "历史"},
            "result": {"action": "remove", "changed": True, "old": "用户偏好通俗历史读物"},
        },
        added("用户对历史感兴趣", changed=False),
        {
            "tool": "memory_ask",
            "arguments": {"question": "要改哪一条？"},
            "result": {"question": "要改哪一条？"},
        },
    ]
    assert notice_texts(records) == [
        "已记住：用户对历史感兴趣",
        "已修改：用户对历史感兴趣 → 用户偏好通俗历史读物",
        "已停止使用这条记忆：用户偏好通俗历史读物。原对话和版本历史仍保留。",
        "这条内容已经在记忆里。",
        "想确认：要改哪一条？",
    ]


def test_notice_texts_show_only_real_failures():
    records = [
        {
            "tool": "memory_add",
            "arguments": {"target": "user", "content": "一条"},
            "error": {"error": "memory_full", "message": "user 记忆需要 1400 个字符，上限为 1375"},
        },
        {
            "tool": "memory_add",
            "arguments": {"target": "user", "content": "另一条"},
            "error": {"error": "memory_store_unavailable", "message": "无法读取 USER.md"},
        },
        {
            "tool": "memory_add",
            "arguments": {"target": "user", "content": "第三条"},
            "error": {"error": "unexpected", "message": "工具执行失败"},
        },
        # invalid_memory 是模型可自行修正的参数问题，不生成提示。
        {
            "tool": "memory_replace",
            "arguments": {"target": "user", "content": "x", "old_text": "不存在"},
            "error": {"error": "invalid_memory", "message": "长期记忆操作未通过校验"},
        },
    ]
    assert notice_texts(records) == [
        "记忆保存失败：user 记忆需要 1400 个字符，上限为 1375",
        "记忆保存失败：无法读取 USER.md",
        "记忆保存失败：工具执行失败",
    ]
