"""长期记忆写入工具：只供一次性记忆会话使用，不进入前台对话的工具列表。

前台主 Agent 不持有记忆工具：每个用户消息轮由独立的一次性判断会话决定写入
（judge_registry），后台定期回顾负责跨轮模式与整理（review_registry）。两者都用同一个
片段编辑工具追加、修改与删除；只有判断会话能向用户追问，回顾独立于对话运行，没有提问的对象。
"""

from server.memory.service import MemoryStore
from server.tools.registry import SideEffect, ToolRegistry


def edit_memory(
    target: str,
    old_text: str = "",
    new_text: str = "",
    operations: list[dict] | None = None,
    *,
    memory_store: MemoryStore,
) -> dict:
    """片段编辑：old_text 为空时追加，new_text 为空时删除，都有时替换；operations 为一组编辑。"""
    return memory_store.edit(target, old_text, new_text, operations)


def ask_memory(question: str) -> dict:
    """判断存在歧义时不做任何写入，把需要向用户确认的问题交回程序。"""
    return {"question": question}


EDIT_DESCRIPTION = (
    "编辑一份长期记忆文档（Markdown）。记忆每轮都会带给助理，只记录跨任务都适用的信息；"
    "某类任务的具体步骤、流程与做法不写入记忆。\n"
    "\n"
    "选择分区：先看这句话写的是用户本人，还是用户以外的事实。\n"
    "- target=user（关于你）：用户是谁。身份、长期目标、学习与研究方向、兴趣、沟通与表达偏好、"
    "工作习惯、对助理的总体期望。例：“用户正在学习 Hermes Agent 的设计”"
    "“用户偏好先给结论再解释”。\n"
    "- target=memory（事实与约定）：用户以外、跨任务都成立的事实与约定。常用账号、日历、"
    "联系人的事实，固定的安排规则，使用各项服务时确认过的经验。例：“‘工作’日历用于内部会议”"
    "“内部会议默认 30 分钟”“对外邀请按对方给出的时长”。\n"
    "写成陈述句记录事实，不写成对自己的命令：写“用户偏好简洁回答”，不写“始终简洁回答”。"
    "涉及条件时保留条件。\n"
    "\n"
    "编辑方式：\n"
    "- 追加：old_text 留空，new_text 是要加入的内容（一行、一个列表项或一小段），加在文末。\n"
    "- 修改：old_text 是当前文档里恰好出现一次的原文片段，new_text 是替换后的文字；"
    "也用于合并重复内容、精简措辞、把新内容并入相关段落。\n"
    "- 删除：old_text 是要删掉的原文（连同列表符号取整行），new_text 留空。原对话仍保留。\n"
    "old_text 必须逐字取自材料里的当前记忆。\n"
    "- 多处编辑：不填 old_text 与 new_text，改为 operations 列表，每项含 old_text 与 new_text，"
    "按顺序作用在同一分区上，整体生效或整体失败，容量只按全部编辑后的结果检查。\n"
    "\n"
    "容量：材料标题写着已用与上限字数。放不下时（或返回 memory_full 后），用一次 operations "
    "调用同时删掉或精简旧内容、加入新内容，不要分几次调用。"
)
OPERATIONS_SCHEMA = {
    "type": "array",
    "description": "多处编辑，按顺序作用在同一分区上，整体生效；使用时不填 old_text 与 new_text",
    "items": {
        "type": "object",
        "properties": {
            "old_text": {"type": "string", "description": "原文片段；留空表示追加"},
            "new_text": {"type": "string", "description": "新文字；留空表示删除"},
        },
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
