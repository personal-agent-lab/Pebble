"""资料说明生成：按标题与正文起草一句话说明，交给用户在编辑页确认后再保存。

说明既进每轮的资料目录，也参与检索，所以要写清讲什么，并带上以后会用来找它的关键词。
"""

from __future__ import annotations

import logging
from typing import Protocol

from server.errors import DependencyUnavailableError, KbValidationError
from server.tools.personal_kb.catalog import SUMMARY_LIMIT

logger = logging.getLogger(__name__)

# 正文只取开头这么多字：说明看整体主题，长文档不必整篇发给模型。
BODY_LIMIT = 12000

SUMMARY_PROMPT = (
    "为一份个人资料写说明。说明会跟在标题后面列入助手每轮看到的资料目录（格式“标题：说明”），"
    "助手据此判断要不要打开这份资料；检索时也会匹配说明里的词。\n"
    "要求：\n"
    "- 不超过 30 个字，用顿号分隔的短语，例如“二期验收结论、代号与遗留问题”。\n"
    "- 写正文实际讲的要点，突出能和同类资料区分开的信息（日期、阶段、对象、结论）。\n"
    "- 尽量保留正文里的专有名词：人名、项目名、代号、编号、地点；标题里已有的词不必重复。\n"
    "- 不写“本资料”“记录了”“可用于”之类的套话，不写正文里没有的内容。\n"
    "- 只输出说明本身，不要引号或前缀。"
)


class TextGenerator(Protocol):
    async def generate_text(self, instructions: str, text: str) -> str: ...


async def generate_summary(gateway: TextGenerator | None, title: str, body: str) -> str:
    if not body.strip():
        raise KbValidationError([{"field": "body", "message": "正文为空，无法生成说明"}])
    if gateway is None:
        raise DependencyUnavailableError("Agent 尚未接入")
    content = body.strip()
    heading = "正文（过长，只给出开头部分）" if len(content) > BODY_LIMIT else "正文"
    text = f"标题：{title.strip() or '（未填写）'}\n\n{heading}：\n{content[:BODY_LIMIT]}"
    try:
        reply = await gateway.generate_text(SUMMARY_PROMPT, text)
    except Exception as error:
        # 模型或网络失败对用户只是“这次没生成出来”，细节留在日志里。
        logger.exception("资料说明生成失败")
        raise DependencyUnavailableError("说明生成失败，请稍后重试") from error
    summary = " ".join(reply.split())
    if not summary:
        raise DependencyUnavailableError("说明生成失败，请稍后重试")
    return summary.strip("“”\"'「」")[:SUMMARY_LIMIT]
