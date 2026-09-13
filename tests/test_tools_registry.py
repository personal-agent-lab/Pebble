"""tests/test_tools_registry.py: 测试工具注册中心与副作用安全隔离。"""

from server.tools.registry import (
    SideEffect,
    ToolRegistry,
    default_registry,
    tool,
)


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


def test_model_exposed_tools_filters_external_write() -> None:
    registry = ToolRegistry()

    @registry.register(side_effect=SideEffect.READONLY)
    def read_tool() -> str:
        return "read"

    @registry.register(side_effect=SideEffect.LOCAL_WRITE)
    def draft_tool() -> str:
        return "draft"

    @registry.register(side_effect=SideEffect.EXTERNAL_WRITE)
    def send_tool() -> str:
        return "sent"

    exposed = registry.get_model_exposed_tools()
    exposed_names = [t.name for t in exposed]

    assert "read_tool" in exposed_names
    assert "draft_tool" in exposed_names
    # 核心安全边界：EXTERNAL_WRITE 绝不能对模型可见
    assert "send_tool" not in exposed_names


def test_global_tool_decorator_registers_to_default_registry() -> None:
    @tool(name="global_read_probe", side_effect=SideEffect.READONLY)
    def probe() -> str:
        return "ok"

    registered = default_registry.get_tool("global_read_probe")
    assert registered is not None
    assert registered.side_effect == SideEffect.READONLY
    assert registered() == "ok"
