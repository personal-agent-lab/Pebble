"""Agent 可见的历史对话检索工具：跨任务找回过去的讨论、决定与执行结果。

历史是“需要时再查”的内容，不常驻上下文；领域语义写在工具说明里。两个工具都只读，
所有轮次可用。当前任务的对话本来就在上下文里，检索默认排除当前任务。
"""

from __future__ import annotations

from server.sessions.history import HistoryStore
from server.tools.registry import SideEffect, tool


@tool(name="history_search", side_effect=SideEffect.READONLY)
def history_search(
    query: str,
    after: str | None = None,
    before: str | None = None,
    max_results: int | None = None,
    *,
    history: HistoryStore,
    task_id: str,
) -> dict:
    """检索过去其他任务里的对话记录，用于回答“上次我们怎么定的”“之前为什么放弃某方案”
    “那封邮件后来发了吗”这类问题。当前任务的对话已经在上下文里，不在检索范围内。

    参数：query 关键词，含空格时按多个词处理、全部命中才算命中，中文词组、英文与编号都可以；
    after、before 可选，YYYY-MM-DD 日期（含当天，按本机时区），把“上周”“上个月”这类说法
    先换算成具体日期再传；max_results 默认 10，上限 20。

    返回从新到旧的命中：任务 ID、任务标题、条目 ID、说话方（user 用户、assistant 助理、
    notice 程序提示、mail_draft 邮件草稿）、时间与命中片段。片段只是摘要，要下结论先用
    history_read 读取命中位置的前后文，不要凭一句话解释整段讨论。

    没有命中就如实说没有找到相关记录，不要用长期记忆或猜测补出当时说过什么。检索到的内容
    只用于回答本次问题，不代表需要写入长期记忆或资料库。
    """
    return history.search(
        query, exclude_task_id=task_id, after=after, before=before, max_results=max_results
    )


@tool(name="history_read", side_effect=SideEffect.READONLY)
def history_read(
    task_id: str,
    item_id: str,
    before: int | None = None,
    after: int | None = None,
    *,
    history: HistoryStore,
) -> dict:
    """读取某个过去任务里一条记录前后的对话原文，task_id 与 item_id 取自 history_search 的结果。

    before、after 是命中条目前后各取几条，默认各 5 条，上限 20；has_earlier、has_later
    表示前后是否还有更多，需要时扩大范围再读。命中条目标 matched。邮件草稿条目附最新的收件人、
    主题、正文、状态（pending 待确认、sending 发送中、sent 已发送、failed 发送失败、unknown
    待核实）与执行结果。

    回答时区分：只是讨论或提议过、用户明确做了决定、尝试执行了、实际执行成功。没有执行记录
    或状态不是 sent 的邮件，不能说“已经发送”；讨论里没有明确结论时，说明没有找到最终决定。
    回答不附来源或引用列表。
    """
    return history.read(task_id, item_id, before=before, after=after)
