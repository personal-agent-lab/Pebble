"""权限边界：声明遗漏、非法组合、前台完整名单与专用会话隔离。"""

import pytest

import server.skills.tools  # noqa: F401 注册技能工具
import server.tools  # noqa: F401 注册领域工具
from server.agent.toolset import TurnKind, exposed_tools
from server.tools.memory.tools import judge_registry, review_registry
from server.tools.registry import Effect, ToolPolicy, ToolRegistry, default_registry, tool


@pytest.mark.parametrize(
    "declaration", [{}, {"effect": Effect.READ_ONLY}, {"policy": ToolPolicy.ALL_TURNS}]
)
def test_declarations_are_required(declaration):
    for register in (ToolRegistry().register, tool):
        with pytest.raises(TypeError):
            register(lambda: None, **declaration)


@pytest.mark.parametrize(
    "effect,policy",
    [
        (Effect.EXTERNAL_WRITE, ToolPolicy.ALL_TURNS),
        (Effect.EXTERNAL_WRITE, ToolPolicy.USER_OR_RESULT_TURN),
        (Effect.EXTERNAL_WRITE, ToolPolicy.DEDICATED_SESSION_ONLY),
        ("read_only", ToolPolicy.ALL_TURNS),
        (Effect.READ_ONLY, "all_turns"),
    ],
)
def test_invalid_declarations_are_rejected(effect, policy):
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(lambda: None, effect=effect, policy=policy)
    assert registry.list_tools() == []


def test_session_scope_is_required_only_for_dedicated_tools():
    with pytest.raises(ValueError):
        ToolRegistry().register(
            lambda: None,
            effect=Effect.LOCAL_WRITE,
            policy=ToolPolicy.DEDICATED_SESSION_ONLY,
        )
    with pytest.raises(ValueError):
        ToolRegistry(session_scope="worker").register(
            lambda: None,
            effect=Effect.READ_ONLY,
            policy=ToolPolicy.ALL_TURNS,
        )


def test_full_foreground_tool_permissions():
    readonly = {
        "gmail_search",
        "gmail_get_thread",
        "gmail_get_message",
        "gmail_get_attachment",
        "gmail_read_draft",
        "calendar_list_events",
        "calendar_get_event",
        "calendar_check_conflicts",
        "kb_search",
        "kb_list",
        "kb_read",
        "kb_history",
        "history_search",
        "history_read",
        "skill_list",
        "skill_view",
    }
    all_writes = {"kb_save", "kb_update"}
    result_writes = {
        "gmail_prepare_reply",
        "gmail_prepare_email",
        "gmail_update_draft",
        "kb_archive",
    }
    user_writes = {"kb_delete", "kb_move", "kb_restore", "skill_manage"}
    expected = {
        TurnKind.NEW_MAIL: readonly | all_writes,
        TurnKind.MESSAGE: readonly
        | all_writes
        | result_writes
        | user_writes
        | {"calendar_create_event"},
        TurnKind.EXECUTION_RESULT: readonly | all_writes | result_writes,
    }
    definitions = [
        t
        for t in default_registry.list_tools()
        if t.func.__module__.startswith(("server.tools.", "server.skills."))
    ]
    for kind, names in expected.items():
        assert {t.name for t in exposed_tools(definitions, kind=kind)} == names
    for definition in definitions:
        if definition.name in readonly:
            assert definition.effect is Effect.READ_ONLY
        elif definition.name == "calendar_create_event":
            assert definition.effect is Effect.EXTERNAL_WRITE
        else:
            assert definition.effect is Effect.LOCAL_WRITE


def test_dedicated_scopes_do_not_leak_into_frontend_or_each_other():
    skill_registry = ToolRegistry(session_scope="skill_review")
    skill_registry.register(
        lambda: None,
        name="skill_propose_change",
        effect=Effect.LOCAL_WRITE,
        policy=ToolPolicy.DEDICATED_SESSION_ONLY,
    )
    dedicated = [
        *judge_registry.list_tools(),
        *review_registry.list_tools(),
        *skill_registry.list_tools(),
    ]
    mixed = [*default_registry.list_tools(), *dedicated]
    for kind in TurnKind:
        assert not any(t.session_scope for t in exposed_tools(mixed, kind=kind))
    for scope, registry in (
        ("memory_judge", judge_registry),
        ("memory_review", review_registry),
        ("skill_review", skill_registry),
    ):
        assert exposed_tools(mixed, session_scope=scope) == registry.list_tools()
    assert exposed_tools(mixed, session_scope="unknown") == []


@pytest.mark.parametrize(
    "context", [{}, {"session_scope": ""}, {"kind": TurnKind.MESSAGE, "session_scope": "worker"}]
)
def test_ambiguous_or_missing_execution_context_is_rejected(context):
    with pytest.raises(ValueError):
        exposed_tools([], **context)
