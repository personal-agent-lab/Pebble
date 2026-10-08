"""前台按需记忆与后台回顾：后台改删整批转为新对话中的候选。"""

from server.memory.notices import MEMORY_RULES
from server.memory.proposals import MemoryProposals, invalid
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from server.tools.registry import Effect, ToolPolicy, ToolRegistry, default_registry


def edit_memory(
    operations: list[dict] | None = None,
    proposal_id: str | None = None,
    decision: str = "view",
    *,
    memory_store: MemoryStore,
    tasks: SessionStore,
    task_id: str,
) -> dict:
    """用户轮读取或编辑；候选对话中的修改必须经过候选版本检查。"""
    proposals = MemoryProposals(memory_store, tasks.path)
    if proposal_id is None and decision != "view":
        raise invalid("处理候选需要 proposal_id")
    if proposal_id is not None:
        return proposals.resolve(task_id, proposal_id, decision, operations)
    if operations is None:
        return proposals.view(task_id)
    pending = proposals.for_task(task_id)
    if pending is not None and pending["status"] == "pending":
        raise invalid("本对话有待处理候选，请传 proposal_id 与 decision，不能绕过版本校验")
    return memory_store.edit(operations)


def review_memory(
    operations: list[dict],
    reason: str = "",
    *,
    memory_store: MemoryStore,
    tasks: SessionStore,
    task_id: str,
) -> dict:
    """后台只自动添加；任何改删或移动均整批暂存，程序创建询问对话。"""
    destructive = any(op.get("action") in {"replace", "delete", "move"} for op in operations)
    if destructive and not reason.strip():
        raise invalid("改删候选必须说明理由")
    preview = memory_store.preview(operations)
    return {
        "planned": True,
        "changed": False,
        "operations": operations,
        "reason": reason,
        "versions": {t: v["version"] for t, v in preview["before"].items()},
        "memory": MemoryProposals(memory_store, tasks.path).view(task_id)["memory"],
    }


# 只讲怎么调用：分区标准、锚点、各 action 与失败处理；写什么、不写什么见 notices.MEMORY_RULES。
EDIT_DESCRIPTION = (
    "编辑长期记忆的两份 Markdown 文档。\n"
    "\n"
    "分区（判据是这句话在做什么，不是说谁）：\n"
    "- user（关于你）：陈述性事实，让助理了解用户与他的生活世界，不含对助理行为的要求。"
    "身份、职业、家庭与身边人、住处、长期目标、学习与研究方向、兴趣、稳定的个人偏好。"
    "例：“用户老家苏州，母亲生日 11 月 8 日”“用户偏好先给结论再解释”。\n"
    "- memory（事实与约定）：约定性内容，用户确立、助理今后要遵守或记住照办的规则、约束、"
    "承诺与默认做法。排事与沟通规则、固定安排、使用各项服务时确认过的经验。"
    "例：“排事避开周二上午”“内部会议默认 30 分钟”“‘工作’日历用于内部会议”。\n"
    "一句话既有背景又有要求时整条进 memory（必要背景写进那一条），纯背景才进 user。\n"
    "\n"
    "材料里每个有文字的行写成“锚点| 原文”，如 `k3f9| - 用户默认使用简体中文。`。"
    "锚点只用来定位，不写进 text。\n"
    "\n"
    "operations 是一组修改，每项写明 action：\n"
    "- append：target + text，加在分区末尾。\n"
    "- insert：after（锚点）+ text，插在那一行下面；和已有内容相关时用它，不为此改写整段。\n"
    "- replace：anchor（可加 end_anchor 表示连续多行）+ text，替换这些行。\n"
    "- delete：anchor（可加 end_anchor），删除这些行。\n"
    "- move：anchor（可加 end_anchor）+ to，把这些行原样移到另一分区末尾；"
    "换分区用它，不拆成删除加追加。\n"
    "text 写完整的行（列表项带“- ”），多行用换行分隔。锚点都指调用前看到的内容；"
    "一次调用可以同时改两个分区，整体生效或整体失败，本轮的修改尽量放进一次调用。\n"
    "\n"
    "结果：成功时返回实际改动和带新锚点的最新内容，之后用新锚点。"
    "返回 invalid_memory（锚点失效或参数不对）时按返回的最新内容重新定位，再调用一次，最多一次；"
    "返回 memory_full 时用一次调用同时精简旧内容、加入新内容；其他失败不重试。"
)
OPERATIONS_SCHEMA = {
    "type": "array",
    "description": "一组修改，锚点都指调用前的内容，整体生效",
    "minItems": 1,
    "items": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["append", "insert", "replace", "delete", "move"],
            },
            "target": {
                "type": "string",
                "enum": ["user", "memory"],
                "description": "append 的分区",
            },
            "after": {"type": "string", "description": "insert：插在这个锚点的行下面"},
            "anchor": {"type": "string", "description": "replace、delete、move 的起始行锚点"},
            "end_anchor": {"type": "string", "description": "可选，连续多行的结束行锚点"},
            "to": {"type": "string", "enum": ["user", "memory"], "description": "move 的目标分区"},
            "text": {
                "type": "string",
                "description": "append、insert、replace 写入的完整行，不带锚点",
            },
        },
        "required": ["action"],
    },
}

FRONT_DESCRIPTION = (
    "用户亲自发起的对话中按需维护长期记忆。只保存用户明确表达的长期信息；"
    "明确纠正和忘记要求可直接修改或删除。拿不准替换还是并存时保持不变，必要时询问。"
    "不能仅凭文字回复声称保存成功，必须以工具结果为准。"
    "不传 operations 时返回最新行锚点与待处理候选，编辑旧记忆前先调用读取。\n"
    + EDIT_DESCRIPTION
    + "\n"
    + MEMORY_RULES
    + "\n"
    "候选对话：用户同意后传 proposal_id、decision=apply（可传用户调整后的完整 operations）；"
    "用户拒绝用 decision=reject。候选不是授权，不得在收到用户同意前应用。"
    "版本冲突时读取最新记忆，使用 decision=refresh 与完整 operations 更新候选、展示差异并再问用户；"
    "新候选不能在 refresh 的同一轮应用。"
)

review_registry = ToolRegistry(session_scope="memory_review")
review_registry.register(
    review_memory,
    name="memory_edit",
    description=EDIT_DESCRIPTION + "\n" + MEMORY_RULES + "\n"
    "调用只校验并登记本次计划，不立即写入；返回的锚点仍是原记忆。"
    "会话成功后统一处理所有调用：纯 append/insert 直接写入；含改删则整批转为候选，提供 reason。"
    "程序自动新建询问对话，用户未回复前原记忆保持不变；planned 不代表写入完成。",
    effect=Effect.LOCAL_WRITE,
    policy=ToolPolicy.DEDICATED_SESSION_ONLY,
    param_schemas={"operations": OPERATIONS_SCHEMA},
)
default_registry.register(
    edit_memory,
    name="memory_edit",
    description=FRONT_DESCRIPTION,
    effect=Effect.LOCAL_WRITE,
    policy=ToolPolicy.USER_TURN_ONLY,
    param_schemas={
        "operations": OPERATIONS_SCHEMA,
        "decision": {
            "type": "string",
            "enum": ["view", "apply", "reject", "refresh"],
            "default": "view",
        },
    },
    activity_renderer=lambda _args: "维护长期记忆",
)
