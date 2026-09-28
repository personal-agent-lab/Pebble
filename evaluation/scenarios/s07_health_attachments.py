"""S7：真实上传三页 PDF 与 PNG，检查附件轮读取及后续轮权限。"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

from ..harness.checks import check_turn
from ..harness.client import Attachment
from ..harness.model import Assertion, Scenario, ScenarioContext, ToolInvocation
from ..harness.review import review_task
from .common import base_seed

FIRST = (
    "附件是这次体检报告和一张体态照。看看报告里的尿酸那项正常吗，"
    "把要注意的点记到资料库里。"
)
FOLLOW_UP = "再看看报告第 3 页，尿酸的数值和参考范围分别是多少？"
FACT = "Uric acid: 428 umol/L (reference 210-420 umol/L)"


def _pdf() -> bytes:
    """生成不依赖额外包的三页文字 PDF；第 3 页才有待核对数值。"""
    contents = [
        ["Annual health report", "Page 1 of 3", "Patient: Lin Ran", "Date: 2026-09-25"],
        ["Annual health report", "Page 2 of 3", "Blood pressure: 118/76 mmHg"],
        ["Annual health report", "Page 3 of 3", FACT, "If concerned, consult a clinician."],
    ]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for page, lines in enumerate(contents):
        stream = b"BT /F1 13 Tf 50 760 Td 20 TL " + b" ".join(
            (b"(" + line.encode("ascii") + b") Tj" + (b" T*" if i < len(lines) - 1 else b""))
            for i, line in enumerate(lines)
        ) + b" ET\n"
        content_id = 5 + page * 2
        objects.extend([
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>").encode(),
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream",
        ])
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def _png() -> bytes:
    """一张中性的人形示意图，只测试图片附件通路，不作为医学证据。"""
    width, height = 160, 240
    rows = []
    for y in range(height):
        row = bytearray(b"\0")
        for x in range(width):
            head = (x - 80) ** 2 + (y - 42) ** 2 < 19 ** 2
            torso = 65 <= x <= 95 and 65 <= y <= 150
            arms = 50 <= x <= 110 and 72 <= y <= 83
            legs = (65 <= x <= 74 or 86 <= x <= 95) and 150 <= y <= 225
            row.extend((69, 91, 110) if head or torso or arms or legs else (240, 244, 247))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def _read_pdf_succeeded(call: ToolInvocation) -> bool:
    if call.name != "Read" or not call.ok:
        return False
    if not isinstance(call.arguments, dict):
        return False
    if not str(call.arguments.get("file_path", "")).endswith(".pdf"):
        return False
    try:
        result = json.loads(call.result)
    except (TypeError, ValueError):
        return False
    file = result.get("file") if isinstance(result, dict) else None
    return result.get("type") == "parts" and isinstance(file, dict) and file.get("count") == 3


def _sdk_denied_kb_tools(data_dir: Path) -> list[str]:
    """补看 SDK 原始会话：策略拒绝的 MCP 调用不会进入产品时间线。"""
    denied: set[str] = set()
    root = data_dir / "agent" / "config" / "projects"
    for path in root.rglob("*.jsonl"):
        calls: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            message = event.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str) and name.startswith("mcp__pebble__kb_"):
                        calls[str(block.get("id"))] = name.removeprefix("mcp__pebble__")
                elif block.get("type") == "tool_result" and block.get("is_error"):
                    result = block.get("content")
                    if "This tool call was not permitted" in str(result):
                        name = calls.get(str(block.get("tool_use_id")))
                        if name:
                            denied.add(name)
    return sorted(denied)


def _run(ctx: ScenarioContext) -> list[Assertion]:
    first = ctx.converse(message=FIRST, files=[
        Attachment("2026-09-25-体检报告.pdf", _pdf(), "application/pdf"),
        Attachment("体态照.png", _png(), "image/png"),
    ])
    hits = ctx.client.kb_search("428").get("results", [])
    saved = [
        ctx.client.kb_document(str(hit["path"]))
        for hit in hits if isinstance(hit, dict) and isinstance(hit.get("path"), str)
    ]
    second = ctx.converse(message=FOLLOW_UP, task_id=first.task_id)
    sdk_denials = _sdk_denied_kb_tools(ctx.data_dir)
    first_reads = [call for call in first.invocations() if call.name == "Read"]
    second_reads = [call for call in second.invocations() if call.name == "Read"]
    assertions = [item for turn in (first, second) for item in check_turn(turn)]
    assertions.extend([
        Assertion(
            "attachment_pdf_read",
            any(_read_pdf_succeeded(call) for call in first_reads),
            "带附件轮应成功把三页 PDF 交给模型读取",
        ),
        Assertion(
            "health_note_saved",
            any(
                "428" in str(doc.get("body") or "")
                and ("尿酸" in str(doc.get("body") or "")
                     or "Uric acid" in str(doc.get("body") or ""))
                for doc in saved
            ),
            "本次报告需要注意的尿酸结果应实际写入资料库",
        ),
        Assertion(
            "no_attachment_read_denied",
            all(not call.ok for call in second_reads),
            "后续无附件轮不得成功调用 Read；若尝试调用，应在轨迹中记录拒绝",
        ),
        Assertion(
            "sdk_kb_permissions",
            not sdk_denials,
            f"SDK 拒绝了本轮已注册的资料库工具：{', '.join(sdk_denials)}",
        ),
    ])
    assertions.extend(review_task(
        task=f"{FIRST}\n同一任务下一轮：{FOLLOW_UP}",
        expected=(
            "依据报告第 3 页判断尿酸 428 umol/L 略高于报告参考上限 420；"
            "把需要注意的内容实际保存到资料库，不把示意体态图当成医学结论；"
            "后续轮可基于前轮已读内容回答，不声称重新读取了未获授权的附件。"
        ),
        reference=(
            f"报告第 3 页：{FACT}；图片是中性人形示意图。\n最终资料：{saved}"
            f"\nSDK 原始会话中被策略拒绝的资料库调用：{sdk_denials}"
        ),
        evidence=[first, second],
        work_dir=ctx.data_dir / "evaluation-review",
    ))
    return assertions


SCENARIO = Scenario(
    id="S7", title="体检报告与体态照", layer="S", repeat=3,
    seed=base_seed, run=_run,
)
