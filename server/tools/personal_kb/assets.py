"""资料正文里的图片：类型识别与上传时的图片说明。

图片存在 `kb/assets/` 下，正文用 `![说明](assets/<名>)` 引用。Agent 不直接看图，
图里的信息靠说明（alt 文本）进入正文，才能被检索与回答。
"""

from __future__ import annotations

import logging
from typing import Protocol

ASSETS_DIR = "assets"
MAX_ASSET_SIZE = 10 * 1024 * 1024
DESCRIPTION_LIMIT = 1000
# 只按文件内容判断类型；不收 SVG（可携带脚本）。
ASSET_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}

DESCRIPTION_PROMPT = (
    "为资料里的一张图片写说明。说明会写进 Markdown 的图片 alt 文本，助手只能通过它知道图里有什么，"
    "检索时也会匹配其中的词。\n"
    "要求：\n"
    "- 图里有文字（截图、表格、板书、票据、文档）时，尽量转写要点：保留专有名词、数字、日期、编号；"
    "表格按“列名：值”逐行压缩。\n"
    "- 没有文字的图，简述画面内容与可辨认的对象。\n"
    "- 只写一行，不超过 800 个字；不写“这张图片显示了”之类的套话。\n"
    "- 看不到图片内容时只输出 NO_IMAGE。\n"
    "- 只输出说明本身，不要引号或前缀。"
)
# 模型看不到图时约定输出的标记：按空说明处理，不把“无法查看图片”之类的话写进正文。
NO_IMAGE = "NO_IMAGE"

logger = logging.getLogger(__name__)


class ImageDescriber(Protocol):
    async def describe_image(self, instructions: str, data: bytes, mime_type: str) -> str: ...


def sniff(data: bytes) -> str | None:
    """按文件头识别图片，返回扩展名；不是支持的类型时返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def clean_description(text: str) -> str:
    """压成一行、去掉会破坏 Markdown 图片语法的方括号，并截断到上限。"""
    flat = " ".join(text.split()).strip("“”\"'「」")
    flat = flat.replace("[", "（").replace("]", "）")
    return flat[:DESCRIPTION_LIMIT]


async def describe(describer: ImageDescriber | None, data: bytes, extension: str) -> str:
    """生成图片说明；未接入模型或调用失败时返回空串，不影响图片保存。"""
    if describer is None:
        return ""
    try:
        reply = await describer.describe_image(DESCRIPTION_PROMPT, data, ASSET_TYPES[extension])
    except Exception:
        logger.exception("图片说明生成失败")
        return ""
    if NO_IMAGE in reply:
        return ""
    return clean_description(reply)
