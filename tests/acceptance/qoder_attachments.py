"""真实 Qoder CN SDK 图片与文件输入验收；显式运行，不进入 pytest。

脚本会调用真实模型并消耗账号额度。图片使用结构化 Base64 输入；文件分别验证 CLI
``--attachment`` 和受控 workspace 的内置 ``Read`` 工具。判定都依赖运行时随机材料，
不能由模型从提示词猜出。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import struct
import tempfile
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from qodercn_agent_sdk import (
    AssistantMessage,
    QoderAgentOptions,
    QoderSDKClient,
    ResultMessage,
    TextBlock,
    access_token,
)

from server.agent.client import CONFIG_DIR_ENV
from server.config import Settings


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def split_color_png() -> bytes:
    """生成左红右蓝的 RGB PNG，不依赖图像处理库或仓库夹具。"""
    width, height = 160, 80
    rows = []
    for _ in range(height):
        pixels = b"\xff\x00\x00" * (width // 2) + b"\x00\x00\xff" * (width // 2)
        rows.append(b"\x00" + pixels)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(b"".join(rows))),
            _png_chunk(b"IEND", b""),
        )
    )


def options(root: Path, settings: Settings, **overrides: Any) -> QoderAgentOptions:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ[CONFIG_DIR_ENV] = str(root / "config")
    tools = overrides.pop("tools", [])
    allowed_tools = overrides.pop("allowed_tools", [])
    return QoderAgentOptions(
        tools=tools,
        allowed_tools=allowed_tools,
        mcp_servers={},
        allowed_mcp_server_names=[],
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        system_prompt="严格按用户要求读取输入并给出简短答案。",
        cwd=workspace,
        include_partial_messages=False,
        auth=access_token(settings.qoder_token.get_secret_value()),
        model=settings.qoder_model,
        **overrides,
    )


async def response_text(client: QoderSDKClient) -> str:
    parts: list[str] = []
    final = ""
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
        elif isinstance(message, ResultMessage):
            if message.is_error:
                raise RuntimeError(message.result or "Qoder 调用失败")
            final = (message.result or "").strip()
    return final or "".join(parts).strip()


async def image_messages() -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "观察随消息发送的图片。按照从左到右的顺序，用大写英文颜色名和连字符"
                        "输出两个主要色块，例如 GREEN-YELLOW。只输出答案；无法读取时输出 "
                        "UNREADABLE。"
                    ),
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(split_color_png()).decode("ascii"),
                    },
                },
            ],
        },
        "parent_tool_use_id": None,
    }


async def verify_image(root: Path, settings: Settings) -> str:
    async with QoderSDKClient(options(root, settings)) as client:
        await client.query(image_messages())
        answer = await response_text(client)
    if "RED-BLUE" not in answer.upper():
        raise AssertionError(f"模型没有正确读取左红右蓝的测试图片：{answer!r}")
    return answer


async def verify_cli_attachment(root: Path, settings: Settings) -> str:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    code = f"FILE-{secrets.token_hex(4).upper()}"
    attachment = workspace / "verification.txt"
    attachment.write_text(
        f"这是文件输入验收材料。唯一校验码：{code}\n",
        encoding="utf-8",
    )
    client = QoderSDKClient(options(root, settings, extra_args={"attachment": str(attachment)}))
    try:
        await client.connect(
            "读取随本轮附加的文本文件，只输出其中以 FILE- 开头的完整校验码。"
            "无法读取时输出 UNREADABLE。"
        )
        answer = await response_text(client)
    finally:
        await client.disconnect()
    if code not in answer:
        raise AssertionError(f"模型没有读出附件中的校验码 {code!r}：{answer!r}")
    return answer


async def verify_workspace_read(root: Path, settings: Settings) -> str:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    code = f"READ-{secrets.token_hex(4).upper()}"
    (workspace / "verification.txt").write_text(
        f"这是 workspace 文件读取验收材料。唯一校验码：{code}\n",
        encoding="utf-8",
    )
    async with QoderSDKClient(
        options(root, settings, tools=["Read"], allowed_tools=["Read"])
    ) as client:
        await client.query(
            "使用 Read 工具读取当前 workspace 中的 verification.txt，"
            "只输出其中以 READ- 开头的完整校验码。"
        )
        answer = await response_text(client)
    if code not in answer:
        raise AssertionError(f"模型没有通过 Read 工具读出校验码 {code!r}：{answer!r}")
    return answer


async def verify(root: Path) -> dict[str, str]:
    settings = Settings()
    if settings.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    report = {
        "structured_image_input": await verify_image(root / "image", settings),
        "workspace_file_read": await verify_workspace_read(root / "read", settings),
    }
    try:
        report["cli_file_attachment"] = await verify_cli_attachment(root / "attachment", settings)
    except AssertionError as error:
        report["cli_file_attachment"] = f"unsupported: {error}"
    return report


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-attachments-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
