"""Skill 文件存储、版本历史与 Git 管理。"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from dataclasses import replace
from functools import wraps
from pathlib import Path
from uuid import uuid4

import yaml

from server.config import get_settings
from server.skills.models import (
    Skill,
    SkillDraft,
    SkillEvidence,
    SkillInput,
    SkillSource,
    SkillStatus,
    SkillVersion,
)
from server.skills.validation import compute_skill_hash, validate_skill_id

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_local = threading.local()


def locked(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        import fcntl

        with _lock:
            if getattr(_local, "active", False):
                return func(*args, **kwargs)
            _data_dir().mkdir(parents=True, exist_ok=True)
            with (_data_dir() / ".skills.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                _local.active = True
                try:
                    _recover()
                    return func(*args, **kwargs)
                finally:
                    _local.active = False
                    fcntl.flock(lock, fcntl.LOCK_UN)

    return wrapped


SKILL_FILENAME = "SKILL.md"


def _data_dir() -> Path:
    return get_settings().data_dir.resolve()


def _skills_dir() -> Path:
    return _data_dir() / "skills"


def _drafts_dir() -> Path:
    return _data_dir() / "skill_drafts"


def _archives_dir() -> Path:
    return _data_dir() / "skill_archives"


def _safe_path(base: Path, child: str) -> Path:
    """校验路径安全：拒绝 ..、符号链接、ID 格式不合法。"""
    if ".." in child or "/" in child or "\\" in child:
        raise ValueError(f"不安全的路径: {child}")
    target = base / child
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError("符号链接不可用于 Skill 路径")
    try:
        target.relative_to(base)
    except ValueError as err:
        raise ValueError(f"路径越界: {child}") from err
    return target


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析 YAML frontmatter 和 Markdown body。"""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()
            return frontmatter, body
    return {}, content


def _render_skill(skill: Skill) -> str:
    """将 Skill 渲染为 YAML frontmatter + Markdown body。"""
    frontmatter: dict = {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "status": skill.status.value,
        "source": skill.source.value,
        "schema_version": skill.schema_version,
    }
    if skill.triggers:
        frontmatter["triggers"] = skill.triggers
    if skill.inputs:
        frontmatter["inputs"] = [
            {"name": inp.name, "required": inp.required, "note": inp.note} for inp in skill.inputs
        ]
    if skill.tools:
        frontmatter["tools"] = skill.tools
    if skill.side_effects:
        frontmatter["side_effects"] = skill.side_effects
    if skill.requires_confirmation:
        frontmatter["requires_confirmation"] = True
    if skill.evidence:
        frontmatter["evidence"] = {
            "tasks": skill.evidence.tasks,
            "occurrences": skill.evidence.occurrences,
            "last_seen_at": skill.evidence.last_seen_at,
        }
    if skill.content_hash:
        frontmatter["content_hash"] = skill.content_hash
    if skill.base_revision:
        frontmatter["base_revision"] = skill.base_revision
    if skill.approved_version:
        frontmatter["approved_version"] = skill.approved_version
    if skill.approved_at:
        frontmatter["approved_at"] = skill.approved_at
    frontmatter["created_at"] = skill.created_at
    frontmatter["updated_at"] = skill.updated_at

    yaml_str = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False).strip()
    return "---\n" + yaml_str + "\n---\n\n" + skill.body


def _skill_from_file(path: Path) -> Skill | None:
    """从文件读取 Skill，返回 None 如果文件不存在或格式错误。"""
    if path.is_symlink():
        raise ValueError("Skill 文件不能为符号链接")
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    frontmatter, body = _parse_frontmatter(content)
    if not frontmatter.get("id"):
        return None

    evidence = None
    ev_data = frontmatter.get("evidence")
    if ev_data and isinstance(ev_data, dict):
        evidence = SkillEvidence(
            tasks=ev_data.get("tasks", []),
            occurrences=ev_data.get("occurrences", 0),
            last_seen_at=ev_data.get("last_seen_at", ""),
        )

    inputs = []
    for inp in frontmatter.get("inputs", []):
        if isinstance(inp, dict):
            inputs.append(
                SkillInput(
                    name=inp.get("name", ""),
                    required=inp.get("required", True),
                    note=inp.get("note", ""),
                )
            )

    return Skill(
        id=frontmatter["id"],
        name=frontmatter.get("name", ""),
        description=frontmatter.get("description", ""),
        status=SkillStatus(frontmatter.get("status", "approved")),
        source=SkillSource(frontmatter.get("source", "user")),
        schema_version=frontmatter.get("schema_version", 1),
        triggers=frontmatter.get("triggers", []),
        inputs=inputs,
        tools=frontmatter.get("tools", []),
        side_effects=frontmatter.get("side_effects", []),
        requires_confirmation=frontmatter.get("requires_confirmation", False),
        evidence=evidence,
        content_hash=frontmatter.get("content_hash", ""),
        base_revision=frontmatter.get("base_revision"),
        approved_version=frontmatter.get("approved_version"),
        approved_at=frontmatter.get("approved_at"),
        body=body,
        created_at=frontmatter.get("created_at", ""),
        updated_at=frontmatter.get("updated_at", ""),
    )


def _write_atomically(path: Path, content: str) -> None:
    """原子化写入：先写暂存文件，再 rename。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _ensure_git_repo() -> None:
    """确保数据目录下的 Git 仓库存在。"""
    repo_dir = _data_dir()
    repo_dir.mkdir(parents=True, exist_ok=True)
    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        try:
            import subprocess

            subprocess.run(
                ["git", "init"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "pebble@local"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Pebble"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning("Git 初始化失败: %s", e)


# ---------- 公开 API ----------


@locked
def load_skill(skill_id: str) -> Skill | None:
    """读取已生效的 Skill。"""
    if not validate_skill_id(skill_id):
        return None
    path = _safe_path(_skills_dir(), skill_id) / SKILL_FILENAME
    return _skill_from_file(path) or _skill_from_file(
        _safe_path(_archives_dir(), skill_id) / SKILL_FILENAME
    )


@locked
def load_draft(draft_id: str) -> SkillDraft | None:
    """读取草稿。"""
    path = _safe_path(_drafts_dir(), draft_id) / SKILL_FILENAME
    if not path.exists():
        return None
    skill = _skill_from_file(path)
    if skill is None:
        return None
    return SkillDraft(
        draft_id=draft_id,
        skill_id=skill.id or None,
        base_revision=skill.base_revision,
        skill=skill,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@locked
def list_drafts() -> list[SkillDraft]:
    """列出所有草稿。"""
    result: list[SkillDraft] = []
    drafts_dir = _drafts_dir()
    if not drafts_dir.exists():
        return result
    for child in sorted(drafts_dir.iterdir()):
        if not child.is_dir():
            continue
        draft = load_draft(child.name)
        if draft is not None and draft.skill.status == SkillStatus.DRAFT:
            result.append(draft)
    return result


@locked
def list_versions(skill_id: str) -> list[SkillVersion]:
    """从 Git 历史读取版本列表。"""
    _ensure_git_repo()
    repo_dir = _data_dir()
    skill_path = _skills_dir() / skill_id / SKILL_FILENAME
    _safe_path(_skills_dir(), skill_id)

    import subprocess

    try:
        result = subprocess.run(
            ["git", "log", "--format=%H|%aI", "--", str(skill_path.relative_to(repo_dir))],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
    except FileNotFoundError:
        return []

    versions: list[SkillVersion] = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        commit_hash, date = line.split("|", 1)
        try:
            file_result = subprocess.run(
                ["git", "show", f"{commit_hash}:{skill_path.relative_to(repo_dir)}"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            if file_result.returncode == 0:
                content = file_result.stdout
                frontmatter, body = _parse_frontmatter(content)
                evidence = None
                ev_data = frontmatter.get("evidence")
                if ev_data and isinstance(ev_data, dict):
                    evidence = SkillEvidence(
                        tasks=ev_data.get("tasks", []),
                        occurrences=ev_data.get("occurrences", 0),
                        last_seen_at=ev_data.get("last_seen_at", ""),
                    )
                inputs = []
                for inp in frontmatter.get("inputs", []):
                    if isinstance(inp, dict):
                        inputs.append(
                            SkillInput(
                                name=inp.get("name", ""),
                                required=inp.get("required", True),
                                note=inp.get("note", ""),
                            )
                        )
                snapshot = Skill(
                    id=frontmatter.get("id", skill_id),
                    name=frontmatter.get("name", ""),
                    description=frontmatter.get("description", ""),
                    status=SkillStatus(frontmatter.get("status", "approved")),
                    source=SkillSource(frontmatter.get("source", "user")),
                    schema_version=frontmatter.get("schema_version", 1),
                    triggers=frontmatter.get("triggers", []),
                    inputs=inputs,
                    tools=frontmatter.get("tools", []),
                    side_effects=frontmatter.get("side_effects", []),
                    requires_confirmation=frontmatter.get("requires_confirmation", False),
                    evidence=evidence,
                    content_hash=frontmatter.get("content_hash", ""),
                    base_revision=frontmatter.get("base_revision"),
                    approved_version=frontmatter.get("approved_version"),
                    approved_at=frontmatter.get("approved_at"),
                    body=body,
                    created_at=frontmatter.get("created_at", ""),
                    updated_at=frontmatter.get("updated_at", ""),
                )
                versions.append(
                    SkillVersion(
                        skill_id=skill_id,
                        revision=frontmatter.get("content_hash", commit_hash[:16]),
                        snapshot=snapshot,
                        approved_at=frontmatter.get("approved_at"),
                        created_at=date,
                    )
                )
        except Exception:
            continue
    return versions


def _recover() -> None:
    import json
    import subprocess

    journal = _data_dir() / ".skills-journal.json"
    if not journal.exists():
        return
    changes = json.loads(journal.read_text())
    committed = True
    for rel, change in changes.items():
        path = Path(rel)
        if path.parts[0] not in {"skills", "skill_drafts", "skill_archives"} or ".." in path.parts:
            raise ValueError("恢复记录路径不合法")
        result = subprocess.run(
            ["git", "show", f"HEAD:{rel}"], cwd=_data_dir(), capture_output=True, text=True
        )
        committed &= (result.stdout if result.returncode == 0 else None) == change["new"]
    for rel, change in changes.items():
        value = change["new"] if committed else change["old"]
        path = _data_dir() / rel
        if value is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomically(path, value)
    subprocess.run(["git", "reset", "--", *changes], cwd=_data_dir(), capture_output=True)
    journal.unlink()


@locked
def _transaction(files: dict[Path, str | None], message: str) -> None:
    import json
    import subprocess

    _ensure_git_repo()
    changes = {}
    for path, content in files.items():
        if path.is_symlink():
            raise ValueError("Skill 文件不能为符号链接")
        old = path.read_text() if path.exists() else None
        if old != content:
            changes[str(path.relative_to(_data_dir()))] = dict(old=old, new=content)
    if not changes:
        return
    journal = _data_dir() / ".skills-journal.json"
    _write_atomically(journal, json.dumps(changes, ensure_ascii=False))
    try:
        for rel, change in changes.items():
            path = _data_dir() / rel
            if change["new"] is None:
                path.unlink(missing_ok=True)
            else:
                _write_atomically(path, change["new"])
        subprocess.run(
            ["git", "add", "--", *changes], cwd=_data_dir(), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "--only", "-m", message, "--", *changes],
            cwd=_data_dir(),
            check=True,
            capture_output=True,
        )
    except BaseException:
        _recover()
        raise
    journal.unlink()


def _persist(path: Path, content: str, message: str) -> None:
    _transaction({path: content}, message)


@locked
def save_skill(skill: Skill) -> str:
    content_hash = compute_skill_hash(skill)
    saved = replace(
        skill, body=skill.body.strip(), content_hash=content_hash, approved_version=content_hash
    )
    _transaction(
        {
            _safe_path(_skills_dir(), skill.id) / SKILL_FILENAME: _render_skill(saved),
            _safe_path(_archives_dir(), skill.id) / SKILL_FILENAME: None,
        },
        f"[Skill] Save {skill.id}",
    )
    return content_hash


@locked
def save_draft(draft: SkillDraft) -> None:
    from server.errors import SkillValidationError
    from server.skills.validation import validate_draft_format

    errors = validate_draft_format(draft)
    if errors:
        raise SkillValidationError(errors)
    skill = replace(
        draft.skill, body=draft.skill.body.strip(), content_hash=compute_skill_hash(draft.skill)
    )
    _persist(
        _safe_path(_drafts_dir(), draft.draft_id) / SKILL_FILENAME,
        _render_skill(skill),
        f"[Skill] Draft {draft.draft_id}",
    )


@locked
def delete_draft(draft_id: str, approved: bool = False) -> None:
    path = _safe_path(_drafts_dir(), draft_id) / SKILL_FILENAME
    draft = load_draft(draft_id)
    if draft:
        # 保留审核历史，归档项不再进入待审核列表。
        _persist(
            path,
            _render_skill(
                replace(
                    draft.skill, status=SkillStatus.APPROVED if approved else SkillStatus.ARCHIVED
                )
            ),
            f"[Skill] Close draft {draft_id}",
        )


@locked
def move_to_archive(skill_id: str) -> None:
    skill = load_skill(skill_id)
    if skill:
        archived = replace(skill, status=SkillStatus.ARCHIVED)
        _transaction(
            {
                _safe_path(_skills_dir(), skill_id) / SKILL_FILENAME: None,
                _safe_path(_archives_dir(), skill_id) / SKILL_FILENAME: _render_skill(archived),
            },
            f"[Skill] Archive {skill_id}",
        )


@locked
def list_all() -> list[Skill]:
    result = []
    for base in (_skills_dir(), _archives_dir()):
        if not base.exists():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and validate_skill_id(child.name):
                try:
                    skill = _skill_from_file(_safe_path(base, child.name) / SKILL_FILENAME)
                    if skill:
                        result.append(skill)
                except (ValueError, yaml.YAMLError):
                    continue
    return result


def generate_id(prefix: str = "sk") -> str:
    """生成唯一 ID。"""
    return f"{prefix}_{uuid4().hex[:12]}"


@locked
def save_status(skill: Skill) -> None:
    """状态变化不能替篡改后的内容重新批准。"""
    _persist(
        _safe_path(_skills_dir(), skill.id) / SKILL_FILENAME,
        _render_skill(skill),
        f"[Skill] Set status {skill.id}",
    )
