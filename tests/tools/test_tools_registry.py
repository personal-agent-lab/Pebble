"""工具注册中心：声明、schema 生成与全局注册。

模型可见范围不在注册表这一层，由 `exposed_tools` 按每轮允许的副作用筛选，
在 tests/gateway/test_integrated_mail.py 覆盖。
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
