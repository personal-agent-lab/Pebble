"""Pebble 工具注册中心与副作用声明。

遵循 Pebble 架构设计：
- 每个工具声明其能力、参数和副作用类型（READONLY / LOCAL_WRITE / EXTERNAL_WRITE）；
- 外部写操作（EXTERNAL_WRITE）绝对不暴露给模型上下文；
- 装饰器 @tool 用于声明与注册工具。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, get_args, get_origin, get_type_hints


class SideEffect(StrEnum):
    """工具副作用声明，程序强制约束，模型不可篡改。"""

    READONLY = "readonly"  # 只读查询，不改变任何系统状态
    LOCAL_WRITE = "local_write"  # 本地写入（如草稿生成、KB 保存）
    EXTERNAL_WRITE = "external_write"  # 外部写入（真实发邮件、建日历），严禁注册给模型！


@dataclass(frozen=True)
class ToolDefinition:
    """结构化工具定义。"""

    name: str
    description: str
    func: Callable[..., Any]
    side_effect: SideEffect
    parameters_schema: dict[str, Any] = field(default_factory=dict)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


class ToolRegistry:
    """进程内工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        side_effect: SideEffect = SideEffect.READONLY,
    ) -> Any:
        """注册工具。可作为普通函数调用，也可作为装饰器使用。"""

        def decorator(fn: Callable[..., Any]) -> ToolDefinition:
            tool_name = name or fn.__name__
            tool_desc = description or inspect.getdoc(fn) or ""
            schema = self._generate_parameters_schema(fn)

            tool_def = ToolDefinition(
                name=tool_name,
                description=tool_desc.strip(),
                func=fn,
                side_effect=side_effect,
                parameters_schema=schema,
            )

            # 附加元数据到原函数
            fn.__pebble_tool__ = tool_def
            self._tools[tool_name] = tool_def
            return tool_def

        if func is not None:
            return decorator(func)
        return decorator

    def get_tool(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self, side_effect: SideEffect | None = None) -> list[ToolDefinition]:
        """列出已注册工具，支持按副作用类型过滤。"""
        if side_effect is None:
            return list(self._tools.values())
        return [t for t in self._tools.values() if t.side_effect == side_effect]

    def get_model_exposed_tools(self) -> list[ToolDefinition]:
        """安全边界：获取对模型可见的工具列表。

        严格过滤掉 EXTERNAL_WRITE，杜绝模型直接调用外部写操作。
        """
        return [t for t in self._tools.values() if t.side_effect != SideEffect.EXTERNAL_WRITE]

    def clear(self) -> None:
        self._tools.clear()

    @staticmethod
    def _generate_parameters_schema(fn: Callable[..., Any]) -> dict[str, Any]:
        """根据函数类型注解与默认值，自动生成轻量 JSON Schema 描述。"""
        sig = inspect.signature(fn)
        type_hints = get_type_hints(fn) if hasattr(fn, "__annotations__") else {}

        properties: dict[str, Any] = {}
        required: list[str] = []

        type_mapping = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }

        for param_name, param in sig.parameters.items():
            # 忽略内部注入参数（如 client, settings 等）
            if param_name in ("self", "cls", "client", "storage"):
                continue

            param_type = type_hints.get(param_name, Any)
            json_type = type_mapping.get(get_origin(param_type) or param_type, "string")

            prop: dict[str, Any] = {"type": json_type}
            if json_type == "array":
                item_type = get_args(param_type)
                prop["items"] = {"type": type_mapping.get(item_type[0], "string")}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            else:
                prop["default"] = param.default

            properties[param_name] = prop

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }


# 全局默认注册表实例
default_registry = ToolRegistry()


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    side_effect: SideEffect = SideEffect.READONLY,
) -> Any:
    """快捷 @tool 装饰器，向全局默认工具注册表注册。"""
    return default_registry.register(
        func, name=name, description=description, side_effect=side_effect
    )
