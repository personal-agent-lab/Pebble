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
import subprocess
import tempfile
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from qodercn_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
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


def split_color_png(left: tuple[int, int, int], right: tuple[int, int, int]) -> bytes:
    """生成左右双色 RGB PNG，不依赖图像处理库或仓库夹具。"""
    width, height = 160, 80
    rows = []
    for _ in range(height):
        pixels = bytes(left) * (width // 2) + bytes(right) * (width // 2)
        rows.append(b"\x00" + pixels)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(b"".join(rows))),
            _png_chunk(b"IEND", b""),
        )
    )


def convert_image(source: Path, target: Path, format_name: str) -> bytes:
    """用系统图像工具生成真实 JPEG/WebP，避免把测试解析器带入产品依赖。"""
    command = (
        ["/opt/homebrew/bin/cwebp", "-lossless", str(source), "-o", str(target)]
        if format_name == "webp"
        else ["/usr/bin/sips", "-s", "format", format_name, str(source), "--out", str(target)]
    )
    subprocess.run(
        command,
        check=True,
        capture_output=True,
    )
    return target.read_bytes()


def simple_pdf(text: str) -> bytes:
    """生成单页、未压缩文本 PDF，供 Runtime 的 Read 工具做真实文档读取。"""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(escaped) + 33} >>\nstream\nBT /F1 18 Tf 72 720 Td "
        f"({escaped}) Tj ET\nendstream",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n{value}\nendobj\n".encode())
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    body.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


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


async def image_messages(images: list[tuple[str, bytes]]) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "观察随消息发送的三张图片，按图片顺序输出每张图从左到右的两个主要"
                        "色块，例如 PNG=RED-BLUE。只输出三行答案；无法读取时输出 UNREADABLE。"
                    ),
                },
                *[
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                    for media_type, data in images
                ],
            ],
        },
        "parent_tool_use_id": None,
    }


async def verify_image(root: Path, settings: Settings) -> str:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    png_path = workspace / "source.png"
    png = split_color_png((255, 0, 0), (0, 0, 255))
    png_path.write_bytes(png)
    jpeg = convert_image(png_path, workspace / "green-yellow.jpg", "jpeg")
    webp_source = split_color_png((255, 0, 255), (0, 255, 255))
    png_path.write_bytes(webp_source)
    webp = convert_image(png_path, workspace / "magenta-cyan.webp", "webp")
    async with QoderSDKClient(options(root, settings)) as client:
        await client.query(
            image_messages(
                [
                    ("image/png", png),
                    ("image/jpeg", jpeg),
                    ("image/webp", webp),
                ]
            )
        )
        answer = await response_text(client)
    expected = ("RED-BLUE", "RED-BLUE", "MAGENTA-CYAN")
    if not all(value in answer.upper() for value in expected):
        raise AssertionError(f"模型没有正确读取 PNG/JPEG/WebP 测试图片：{answer!r}")
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
    codes = {
        "verification.txt": f"TXT-{secrets.token_hex(4).upper()}",
        "verification.md": f"MD-{secrets.token_hex(4).upper()}",
        "verification.py": f"PY-{secrets.token_hex(4).upper()}",
        "verification.pdf": f"PDF-{secrets.token_hex(4).upper()}",
    }
    for filename, code in codes.items():
        target = workspace / filename
        if target.suffix == ".pdf":
            target.write_bytes(simple_pdf(code))
        else:
            target.write_text(f"唯一校验码：{code}\n", encoding="utf-8")

    async def authorize_read(tool_name: str, tool_input: dict, _context: Any):
        if tool_name != "Read" or not isinstance(tool_input.get("file_path"), str):
            return PermissionResultDeny(message="只允许读取本次验收 workspace 内的文件")
        requested = Path(tool_input["file_path"])
        requested = requested if requested.is_absolute() else workspace / requested
        try:
            requested.resolve().relative_to(workspace.resolve())
        except ValueError:
            return PermissionResultDeny(message="文件不在本次验收 workspace 内")
        return PermissionResultAllow()

    async with QoderSDKClient(
        options(
            root,
            settings,
            tools=["Read"],
            allowed_tools=["Read"],
            can_use_tool=authorize_read,
        )
    ) as client:
        await client.query(
            "使用 Read 工具逐个读取当前 workspace 中的 verification.txt、verification.md、"
            "verification.py、verification.pdf，只输出每个文件中的完整校验码，每行一个。"
        )
        answer = await response_text(client)
    missing = [code for code in codes.values() if code not in answer]
    if missing:
        raise AssertionError(f"模型没有通过 Read 工具读出校验码 {missing!r}：{answer!r}")
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
