"""Pebble 工具注册中心与副作用声明。

遵循 Pebble 架构设计：
- 每个工具声明其能力、参数和副作用类型（READONLY / LOCAL_WRITE / EXTERNAL_WRITE）；
- 外部写操作（EXTERNAL_WRITE）绝对不暴露给模型上下文；
- 装饰器 @tool 用于声明与注册工具。

注册表是装配的唯一来源：`agent/toolset.py` 遍历已注册工具绑定依赖，模型可见范围由
`agent/toolset.py` 的 `exposed_tools` 按本轮允许的副作用筛选。注册表本身不做筛选，
避免两套宽严不同的边界。
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
    # 声明了仅关键字 task_id 参数的工具由网关在每轮调用时注入任务身份，不进入模型 schema。
    needs_task_id: bool = False
    # 保存成功后需要通知页面可读取草稿的工具；网关在调用成功后据此发 draft_saved 事件。
    emits_draft_saved: bool = False

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
        emits_draft_saved: bool = False,
    ) -> Any:
        """注册工具。可作为普通函数调用，也可作为装饰器使用。"""

        def decorator(fn: Callable[..., Any]) -> ToolDefinition:
            tool_name = name or fn.__name__
            tool_desc = description or inspect.getdoc(fn) or ""
            schema = self._generate_parameters_schema(fn)
            parameters = inspect.signature(fn).parameters
            needs_task_id = (
                "task_id" in parameters
                and parameters["task_id"].kind is inspect.Parameter.KEYWORD_ONLY
            )

            tool_def = ToolDefinition(
                name=tool_name,
                description=tool_desc.strip(),
                func=fn,
                side_effect=side_effect,
                parameters_schema=schema,
                needs_task_id=needs_task_id,
                emits_draft_saved=emits_draft_saved,
            )

            self._tools[tool_name] = tool_def
            return tool_def

        if func is not None:
            return decorator(func)
        return decorator

    def get_tool(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """全部已注册工具，按注册顺序。

        模型可见范围不在这里决定：注册表只记录声明，装配交给 `agent/toolset.py`，
        每轮的允许集合由它的 `exposed_tools` 按副作用声明筛选，全流程只有那一处筛选。
        """
        return list(self._tools.values())

    @staticmethod
    def _generate_parameters_schema(fn: Callable[..., Any]) -> dict[str, Any]:
        """根据函数类型注解与默认值，自动生成轻量 JSON Schema 描述。

        仅关键字参数不进入模型可见的 schema：客户端、存储等装配依赖由 `agent/toolset.py`
        在装配时绑定；`task_id` 是每轮调用时由网关注入的任务身份。模型可见参数一律声明为
        位置或关键字参数。
        """
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
            if param_name in ("self", "cls") or param.kind is inspect.Parameter.KEYWORD_ONLY:
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
    emits_draft_saved: bool = False,
) -> Any:
    """快捷 @tool 装饰器，向全局默认工具注册表注册。"""
    return default_registry.register(
        func,
        name=name,
        description=description,
        side_effect=side_effect,
        emits_draft_saved=emits_draft_saved,
    )
