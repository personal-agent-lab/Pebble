"""S3：用户用编辑器直改资料，检查收编、检索和可恢复的历史。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..harness.checks import check_turn
from ..harness.model import Assertion, Scenario, ScenarioContext, TurnEvidence
from ..harness.review import review_task
from ..harness.seeds import KbDoc, SeedBuilder
from .common import seed_corpus

OLD = "kb/财务/保险单据.md"
RENAMED = "kb/财务/家庭保险.md"
COPY = "kb/财务/家庭保险 备份.md"
TRIP = "kb/2026 国庆出行计划.md"
LEASE = "kb/财务/2025 房租合同.md"
QUESTION = "我刚用编辑器整理了资料库。国庆的出行计划存哪了？"


def _seed(builder: SeedBuilder) -> None:
    documents, user_md, memory_md = seed_corpus()
    initial = []
    for doc in documents:
        if doc.title in {"2026 国庆出行计划", "家庭保险 备份"}:
            continue
        if doc.title == "家庭保险":
            initial.append(KbDoc(
                title="保险单据", directory=doc.directory,
                body=doc.body.replace("# 家庭保险", "# 保险单据", 1),
                summary=doc.summary,
            ))
        else:
            initial.append(doc)
    builder.memory(user_md, memory_md)
    builder.kb_docs(initial)


def _retitle(raw: str, old: str, new: str) -> str:
    metadata = f"title: {old}"
    heading = f"# {old}"
    if raw.count(metadata) != 1 or raw.count(heading) != 1:
        raise RuntimeError(f"S3 无法在用户文件中定位标题 {old}")
    return raw.replace(metadata, f"title: {new}", 1).replace(heading, f"# {new}", 1)


def _git(data_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(data_dir), *args], text=True, capture_output=True,
        check=True,
    )
    return result.stdout


def _deleted_recoverable(data_dir: Path) -> bool:
    deletion = _git(data_dir, "log", "-1", "--diff-filter=D", "--format=%H", "--", LEASE).strip()
    if not deletion:
        return False
    prior = _git(data_dir, "show", f"{deletion}^:{LEASE}")
    return "# 2025 房租合同" in prior and "徐师傅" in prior


def _move_history_preserved(data_dir: Path) -> bool:
    history = _git(
        data_dir, "-c", "core.quotepath=false", "log", "--follow", "--name-status",
        "--format=%s", "--", RENAMED,
    )
    return OLD in history and RENAMED in history and "R100" in history


def _agent_found_document(evidence: TurnEvidence) -> bool:
    for call in evidence.invocations():
        if not call.ok or call.name not in {"kb_list", "kb_search", "kb_read"}:
            continue
        try:
            result = json.loads(call.result)
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        candidates = (
            result.get("documents", []) if call.name == "kb_list"
            else result.get("results", []) if call.name == "kb_search"
            else [result]
        )
        if isinstance(candidates, list) and any(
            isinstance(item, dict) and item.get("path") == TRIP for item in candidates
        ):
            return True
    return False


def _run(ctx: ScenarioContext) -> list[Assertion]:
    # 第一次正常资料读取让初始种子完成 intake，之后的磁盘操作才是用户的新动作。
    original = ctx.client.kb_document(OLD)
    old_id = str(original["id"])
    old_file = ctx.data_dir / OLD
    renamed_file = ctx.data_dir / RENAMED
    old_file.rename(renamed_file)
    renamed_file.write_text(
        _retitle(renamed_file.read_text(encoding="utf-8"), "保险单据", "家庭保险"),
        encoding="utf-8",
    )
    moved = ctx.client.kb_document(RENAMED)  # 触发移动收编，再复制该文件。

    copied_file = ctx.data_dir / COPY
    shutil.copyfile(renamed_file, copied_file)
    copied_file.write_text(
        _retitle(copied_file.read_text(encoding="utf-8"), "家庭保险", "家庭保险 备份"),
        encoding="utf-8",
    )
    documents, _, _ = seed_corpus()
    trip = next(doc for doc in documents if doc.title == "2026 国庆出行计划")
    (ctx.data_dir / TRIP).write_text(trip.body.strip() + "\n", encoding="utf-8")
    (ctx.data_dir / LEASE).unlink()
    listing = ctx.client.kb_documents()  # 收编新增、复制、删除。
    copied = ctx.client.kb_document(COPY)
    found = ctx.client.kb_search("国庆出行计划")
    paths = {str(doc["path"]) for doc in listing.get("documents", [])}
    hits = {str(hit["path"]) for hit in found.get("results", [])}

    evidence = ctx.converse(message=QUESTION)
    assertions = check_turn(evidence)
    assertions.extend([
        Assertion("intake_new_document", TRIP in paths and TRIP in hits,
                  "新建出行计划应被收编并可检索"),
        Assertion("move_keeps_identity",
                  moved["id"] == old_id and OLD not in paths
                  and _move_history_preserved(ctx.data_dir),
                  "改名后保留原文档 id，旧路径不再列出且跨路径历史可追溯"),
        Assertion("copy_gets_new_identity", COPY in paths and copied["id"] != old_id,
                  "复制件应取得新 id"),
        Assertion("delete_is_recoverable",
                  LEASE not in paths and _deleted_recoverable(ctx.data_dir),
                  "过期合同不再列出，删除前正文仍在本地版本历史中"),
        Assertion("agent_looked_up_current_document", _agent_found_document(evidence),
                  "助手应从资料库找到用户新增的出行计划"),
    ])
    assertions.extend(review_task(
        task=QUESTION,
        expected=(
            "回答计划在资料库《2026 国庆出行计划》，可指出文件路径；"
            "不得说找不到或引用已删除合同。"
        ),
        reference=f"当前文件列表：{sorted(paths)}\n出行计划搜索结果：{found}",
        evidence=[evidence],
        work_dir=ctx.data_dir / "evaluation-review",
    ))
    return assertions


SCENARIO = Scenario(
    id="S3", title="编辑器直改资料库", layer="S", repeat=3, seed=_seed, run=_run,
)
