"""工具注册声明：业务副作用与开放策略分别显式声明。

注册时校验合法组合和专用会话范围；模型可见工具由 agent/toolset.py 统一筛选。
外部服务执行函数不注册为模型工具，仍由 Confirmation 管理。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, get_args, get_origin, get_type_hints


class Effect(StrEnum):
    """业务副作用；内部观测与缓存不改变只读工具的分类。"""

    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"


class ToolPolicy(StrEnum):
    """模型工具的开放策略，不能替代具体操作的领域授权校验。"""

    ALL_TURNS = "all_turns"
    USER_OR_RESULT_TURN = "user_or_result_turn"
    USER_TURN_ONLY = "user_turn_only"
    DEDICATED_SESSION_ONLY = "dedicated_session_only"


VALID_POLICIES: dict[Effect, frozenset[ToolPolicy]] = {
    Effect.READ_ONLY: frozenset({ToolPolicy.ALL_TURNS, ToolPolicy.DEDICATED_SESSION_ONLY}),
    Effect.LOCAL_WRITE: frozenset(ToolPolicy),
    Effect.EXTERNAL_WRITE: frozenset({ToolPolicy.USER_TURN_ONLY}),
}


@dataclass(frozen=True)
class ToolFileResult:
    """工具返回的文件。

    结构化信息作为文本交给 Agent，原始字节作为文件内容交付，
    避免把大段 base64 当普通文本塞进模型上下文。
    """

    uri: str
    filename: str
    mime_type: str
    data: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ToolDefinition:
    """结构化工具定义。"""

    name: str
    description: str
    func: Callable[..., Any]
    effect: Effect
    policy: ToolPolicy
    session_scope: str | None = None
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    # 声明了仅关键字 task_id 参数的工具由网关在每轮调用时注入任务身份，不进入模型 schema。
    needs_task_id: bool = False
    # 保存成功后需要通知页面可读取草稿的工具；网关在调用成功后据此发 draft_saved 事件。
    emits_draft_saved: bool = False
    # 工具成功后的用户可见提示由所属领域生成；通用工具边界只负责转发文本。
    notice_renderer: Callable[[dict[str, Any]], str] | None = None
    # 模型发起调用时向用户说明“正在做什么”，输入是模型给出的参数；措辞由所属领域提供。
    activity_renderer: Callable[[dict[str, Any]], str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect, Effect) or not isinstance(self.policy, ToolPolicy):
            raise ValueError("工具必须显式声明 Effect 与 ToolPolicy 枚举")
        if self.policy not in VALID_POLICIES[self.effect]:
            raise ValueError(f"不允许的工具权限组合：{self.effect}/{self.policy}")
        if self.session_scope is not None and (
            not isinstance(self.session_scope, str) or not self.session_scope.strip()
        ):
            raise ValueError("专用会话范围必须是非空字符串")
        dedicated = self.policy is ToolPolicy.DEDICATED_SESSION_ONLY
        if dedicated != bool(self.session_scope):
            raise ValueError("专用工具必须绑定专用会话范围，前台工具不得绑定该范围")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


class ToolRegistry:
    """进程内工具注册表。"""

    def __init__(self, *, session_scope: str | None = None) -> None:
        self.session_scope = session_scope
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        effect: Effect,
        policy: ToolPolicy,
        emits_draft_saved: bool = False,
        notice_renderer: Callable[[dict[str, Any]], str] | None = None,
        activity_renderer: Callable[[dict[str, Any]], str] | None = None,
        param_schemas: dict[str, dict[str, Any]] | None = None,
    ) -> Any:
        """注册工具。可作为普通函数调用，也可作为装饰器使用。

        `param_schemas` 按参数名覆盖自动生成的 schema：自动映射只能给嵌套结构最粗的
        类型（如 object），声明了必填字段与嵌套形状的参数由工具自行提供。
        """

        def decorator(fn: Callable[..., Any]) -> ToolDefinition:
            tool_name = name or fn.__name__
            tool_desc = description or inspect.getdoc(fn) or ""
            schema = self._generate_parameters_schema(fn)
            for param_name, replacement in (param_schemas or {}).items():
                if param_name in schema["properties"]:
                    schema["properties"][param_name] = replacement
            parameters = inspect.signature(fn).parameters
            needs_task_id = (
                "task_id" in parameters
                and parameters["task_id"].kind is inspect.Parameter.KEYWORD_ONLY
            )

            tool_def = ToolDefinition(
                name=tool_name,
                description=tool_desc.strip(),
                func=fn,
                effect=effect,
                policy=policy,
                session_scope=self.session_scope,
                parameters_schema=schema,
                needs_task_id=needs_task_id,
                emits_draft_saved=emits_draft_saved,
                notice_renderer=notice_renderer,
                activity_renderer=activity_renderer,
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
        每轮的允许集合由它的 `exposed_tools` 按开放策略筛选，全流程只有那一处筛选。
        """
        return list(self._tools.values())

    @staticmethod
    def _concrete_type(param_type: Any) -> Any:
        """剥离 `Optional[X]` / `X | None`，返回真实类型 X。

        可选的数组/对象参数若直接取 `get_origin` 会得到 UnionType，落到默认 string；
        先解包出唯一的非 None 成员，才能映射成正确的 array/object。
        """
        args = get_args(param_type)
        if args and type(None) in args:
            non_null = [arg for arg in args if arg is not type(None)]
            if len(non_null) == 1:
                return non_null[0]
        return param_type

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

            param_type = ToolRegistry._concrete_type(type_hints.get(param_name, Any))
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


# 步骤说明里参数的最长显示长度：足够认出在查什么，又不把一行撑长。
ACTIVITY_DETAIL_LIMIT = 40


def activity(label: str, detail: object = None) -> str:
    """拼一条步骤说明：“动作：对象”，对象取自模型参数，过长截断，缺省时只有动作。"""
    text = " ".join(str(detail).split()) if isinstance(detail, (str, int, float)) else ""
    if not text:
        return label
    if len(text) > ACTIVITY_DETAIL_LIMIT:
        text = text[:ACTIVITY_DETAIL_LIMIT] + "…"
    return f"{label}：{text}"


# 全局默认注册表实例
default_registry = ToolRegistry()


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    effect: Effect,
    policy: ToolPolicy,
    emits_draft_saved: bool = False,
    notice_renderer: Callable[[dict[str, Any]], str] | None = None,
    activity_renderer: Callable[[dict[str, Any]], str] | None = None,
    param_schemas: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """快捷 @tool 装饰器，向全局默认工具注册表注册。"""
    return default_registry.register(
        func,
        name=name,
        description=description,
        effect=effect,
        policy=policy,
        emits_draft_saved=emits_draft_saved,
        notice_renderer=notice_renderer,
        activity_renderer=activity_renderer,
        param_schemas=param_schemas,
    )
