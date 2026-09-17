"""长期记忆写入工具：只供一次性记忆会话使用，不进入前台对话的工具列表。

前台主 Agent 不持有记忆工具：每个用户消息轮由独立的一次性判断会话决定写入
（judge_registry），后台定期回顾负责跨轮模式与整理（review_registry）。两者都用同一个
按行锚点编辑的工具追加、插入、修改、删除与移动；只有判断会话能向用户追问，回顾独立于对话运行，没有提问的对象。
"""

from server.memory.service import MemoryStore
from server.tools.registry import SideEffect, ToolRegistry


def edit_memory(operations: list[dict], *, memory_store: MemoryStore) -> dict:
    """按行锚点插入、替换、删除、移动整行或在分区末尾追加；一次调用可跨分区，整体生效。"""
    return memory_store.edit(operations)


def ask_memory(question: str) -> dict:
    """判断存在歧义时不做任何写入，把需要向用户确认的问题交回程序。"""
    return {"question": question}


EDIT_DESCRIPTION = (
    "编辑长期记忆（两份 Markdown 文档）。记忆每轮都会带给助理，只记录跨任务都适用的信息；"
    "某类任务的具体步骤、流程与做法不写入记忆。\n"
    "\n"
    "选择分区：先看这句话写的是用户本人，还是用户以外的事实。\n"
    "- user（关于你）：用户是谁。身份、长期目标、学习与研究方向、兴趣、沟通与表达偏好、"
    "工作习惯、对助理的总体期望。例：“用户正在学习 Hermes Agent 的设计”"
    "“用户偏好先给结论再解释”。\n"
    "- memory（事实与约定）：用户以外、跨任务都成立的事实与约定。常用账号、日历、"
    "联系人的事实，固定的安排规则，使用各项服务时确认过的经验。例：“‘工作’日历用于内部会议”"
    "“内部会议默认 30 分钟”“对外邀请按对方给出的时长”。\n"
    "写成陈述句记录事实，不写成对自己的命令：写“用户偏好简洁回答”，不写“始终简洁回答”。"
    "涉及条件时保留条件。\n"
    "\n"
    "读懂材料：当前记忆每个有文字的行写成“锚点| 原文”，例如 `k3f9| - 用户默认使用简体中文。`"
    "里 k3f9 是锚点。锚点只用来指明是哪一行，不是记忆内容，不要写进 text。空行没有锚点。\n"
    "\n"
    "参数只有 operations：一组修改，只改一处也放在列表里，每项写明 action：\n"
    "- append：target（user 或 memory）+ text，加在该分区末尾。\n"
    "- insert：after（锚点）+ text，作为新的行插在那一行下面。新内容和某条已有内容相关时用它，"
    "不要为此改写整段。\n"
    "- replace：anchor（可加 end_anchor 表示连续多行）+ text，用 text 替换这些整行。\n"
    "- delete：anchor（可加 end_anchor），删除这些行。原对话仍保留。\n"
    "- move：anchor（可加 end_anchor）+ to（另一个分区），把这些行原样移到另一个分区末尾。\n"
    "text 写完整的行（列表项带上“- ”）；多行用换行分隔。\n"
    "一次调用里的锚点都指调用前看到的内容，可以同时改两个分区，整体生效或整体失败；"
    "本轮要做的修改尽量放进一次调用。\n"
    "\n"
    "结果：成功时返回实际改动与改动分区带新锚点的最新内容，之后的调用用新锚点。"
    "返回 invalid_memory 时（锚点失效或参数不对）按返回的最新内容重新定位，再调用一次。\n"
    "容量：材料标题写着已用与上限字数。放不下时（或返回 memory_full 后），用一次调用"
    "同时删掉或精简旧内容、加入新内容。"
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

review_registry = ToolRegistry()
judge_registry = ToolRegistry()

for registry in (judge_registry, review_registry):
    registry.register(
        edit_memory,
        name="memory_edit",
        description=EDIT_DESCRIPTION,
        side_effect=SideEffect.LOCAL_WRITE,
        param_schemas={"operations": OPERATIONS_SCHEMA},
    )

judge_registry.register(
    ask_memory,
    name="memory_ask",
    description=(
        "判断存在歧义时向用户提出一句具体的确认问题，不做任何写入。"
        "问题要指出候选内容或两种理解，让用户一句话即可回答。"
    ),
    side_effect=SideEffect.READONLY,
)
