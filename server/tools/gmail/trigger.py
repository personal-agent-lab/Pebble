"""新邮件触发的 Agent 输入内容：触发文案与载荷属于邮件域，不进入通用层。

调度层（`gateway/runtime.py`）按调用种类取出本模块组装的消息与材料，构造当轮输入。
"""

from server.agent.context import Material

NEW_MAIL_GOAL = "处理新收到的邮件"
NEW_MAIL_MESSAGE = (
    "收到新邮件，邮件与线程标识见系统提示。"
    "请阅读后用一段连续的文字向用户说明这封邮件，按邮件的价值决定详略；"
    "用户没有明确要求之前不要起草回复。"
)
NEW_MAIL_MATERIAL_TITLE = "本轮触发：新邮件"


def new_mail_content(*, source_message_id: str, thread_id: str) -> tuple[str, tuple[Material, ...]]:
    """新邮件轮的消息与材料：标识走系统提示的 JSON 块，消息本身不嵌标识。"""
    return NEW_MAIL_MESSAGE, (
        Material(
            NEW_MAIL_MATERIAL_TITLE,
            {"source_message_id": source_message_id, "thread_id": thread_id},
        ),
    )
