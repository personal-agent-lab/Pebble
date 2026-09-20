"""工具注册中心：声明、schema 生成与全局注册。

模型可见范围不在注册表这一层，由 `server/agent/toolset.py` 按每轮允许的副作用筛选，
在 tests/gateway/test_agent_stream.py 覆盖。
"""

from server.tools.registry import SideEffect, ToolRegistry, default_registry, tool


def test_tool_decorator_registers_metadata() -> None:
    registry = ToolRegistry()

    @registry.register(
        name="test_calc",
        description="计算两数之和",
        side_effect=SideEffect.READONLY,
    )
    def add(a: int, b: int = 1) -> int:
        return a + b

    tool_def = registry.get_tool("test_calc")
    assert tool_def is not None
    assert tool_def.name == "test_calc"
    assert tool_def.description == "计算两数之和"
    assert tool_def.side_effect == SideEffect.READONLY
    assert tool_def(2, 3) == 5

    # 验证 schema 生成
    schema = tool_def.parameters_schema
    assert schema["type"] == "object"
    assert "a" in schema["properties"]
    assert "b" in schema["properties"]
    assert schema["required"] == ["a"]


def test_global_tool_decorator_registers_to_default_registry() -> None:
    @tool(name="global_read_probe", side_effect=SideEffect.READONLY)
    def probe() -> str:
        return "ok"

    registered = default_registry.get_tool("global_read_probe")
    assert registered is not None
    assert registered.side_effect == SideEffect.READONLY
    assert registered() == "ok"


def test_step_descriptions_are_short_and_come_from_domain_tools():
    from server.tools.registry import activity, default_registry

    assert activity("正在检索资料") == "正在检索资料"
    assert activity("正在检索资料", "  星云\n验收 ") == "正在检索资料：星云 验收"
    assert activity("正在检索资料", "长" * 50) == "正在检索资料：" + "长" * 40 + "…"
    assert activity("正在检索资料", {"不是": "文本"}) == "正在检索资料"

    def describe(name, arguments):
        return default_registry.get_tool(name).activity_renderer(arguments)

    ref = {"path": "kb/项目/验收.md", "commit": "c", "lines": [1, 2]}
    assert describe("kb_read", {"ref": ref}) == "正在读取资料：项目/验收.md"
    assert describe("kb_read", {"id": "kb_123"}) == "正在读取资料"
    assert describe("kb_list", {"deleted": True}) == "正在查看已删除的资料"
    assert describe("calendar_check_conflicts", {
        "start": "2026-09-17T15:00:00+08:00", "end": "2026-09-17T16:00:00+08:00",
    }) == "正在检查日程冲突：2026-09-17 15:00 至 2026-09-17 16:00"
    assert describe("gmail_prepare_reply", {"subject": "回复：邀请"}) == "正在起草回复：回复：邀请"
    assert describe("history_search", {"query": "放弃 A 方案"}) == "正在检索过去的对话：放弃 A 方案"
    # 模型可见的前台工具都给出步骤说明
    import server.tools  # noqa: F401  注册全部领域工具

    missing = [
        tool.name
        for tool in default_registry.list_tools()
        if tool.func.__module__.startswith("server.tools.") and tool.activity_renderer is None
    ]
    assert missing == []
