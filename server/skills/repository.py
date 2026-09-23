"""技能的文件与 Git 存储：`skills/<id>/` 下正文、附件与版本历史。

与资料库共用实例数据目录里的同一个本地 Git 仓库和进程内锁，另加跨进程文件锁；
文件写入与提交在同一临界区完成，提交失败恢复原文件，不留半完成状态。
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

from server.errors import SkillStoreUnavailableError, SkillUnknownError
from server.skills.models import (
    SKILL_ID_PATTERN,
    Skill,
    SkillFile,
    SkillOrigin,
    SkillState,
    SkillVersion,
    content_hash,
)
from server.storage.datarepo import GITIGNORE, lock_for

logger = logging.getLogger(__name__)

SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
ATTACHMENT_PREFIXES = ("references/", "templates/")
FRONTMATTER_FIELDS = ("name", "description", "origin", "managed", "state")

# 提交说明的元数据走尾注，`versions()` 用同一正则读回。
_TRAILER = re.compile(r"^(change-id|actor|reason): (.*)$", re.MULTILINE)


def now() -> str:
    return datetime.now(UTC).isoformat()


def safe_attachment_path(relative: str) -> str:
    """附件只能是 `references/`、`templates/` 下的相对路径，逐段检查不允许 `..`。"""

    target = PurePosixPath(relative)
    if target.is_absolute() or not str(target).startswith(ATTACHMENT_PREFIXES):
        raise ValueError(f"附件路径不合法：{relative}")
    if any(part in ("", ".", "..") for part in target.parts):
        raise ValueError(f"附件路径不合法：{relative}")
    return str(target)


def skill_dir_path(skill_id: str) -> str:
    """仓库相对的技能目录路径；只接受目录名形式的标识。"""

    if not SKILL_ID_PATTERN.match(skill_id):
        raise ValueError(f"技能标识不合法：{skill_id}")
    return f"{SKILLS_DIR}/{skill_id}"


def render_skill_md(skill: Skill) -> bytes:
    frontmatter = {
        "name": skill.name,
        "description": skill.description,
        "origin": skill.origin.value,
        "managed": skill.managed,
        "state": skill.state.value,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
    }
    head = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{head}---\n\n{skill.body}".encode()


def parse_skill_md(raw: str) -> tuple[dict, str]:
    """拆出 frontmatter 与正文；格式不对时抛 `ValueError`，由调用方决定如何处置。"""

    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("缺少 frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("frontmatter 未闭合")
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"frontmatter 无法解析：{error}") from error
    if not isinstance(frontmatter, dict):
        raise ValueError("frontmatter 不是键值对")
    if not all(field in frontmatter for field in FRONTMATTER_FIELDS):
        raise ValueError("frontmatter 缺少必填字段")
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return frontmatter, body


class SkillRepository:
    """技能的文件与版本仓库；所有写操作在进程锁与跨进程文件锁内完成。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.skills_root = self.data_dir / SKILLS_DIR
        self._lock = lock_for(self.data_dir)
        self._ready = False

    # ------------------------------------------------------------------ 读取

    def load(self, skill_id: str) -> Skill | None:
        try:
            raw = (self.skills_root / skill_id / SKILL_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        try:
            return self._build_skill(skill_id, raw, self._scan_files(skill_id))
        except ValueError as error:
            logger.warning("技能 %s 的 SKILL.md 不合法，退出目录：%s", skill_id, error)
            return None

    def load_all(self) -> list[Skill]:
        """可加载的全部技能；正文不合法或工作区相对 Git 有未提交改动的退出目录并告警。"""

        self.initialize()
        if not self.skills_root.is_dir():
            return []
        dirty = self._dirty_skill_ids()
        skills: list[Skill] = []
        for entry in sorted(self.skills_root.iterdir()):
            if not entry.is_dir() or not SKILL_ID_PATTERN.match(entry.name):
                continue
            if entry.name in dirty:
                logger.warning("技能 %s 有未提交的磁盘改动，退出目录待处理", entry.name)
                continue
            skill = self.load(entry.name)
            if skill is not None:
                skills.append(skill)
        return skills

    def read_attachment(self, skill_id: str, relative_path: str) -> bytes:
        try:
            target = self.skills_root / skill_id / safe_attachment_path(relative_path)
        except ValueError as error:
            raise SkillUnknownError(str(error)) from error
        resolved = target.resolve()
        skill_root = (self.skills_root / skill_id).resolve()
        if skill_root not in resolved.parents:
            raise SkillUnknownError(f"附件路径越界：{relative_path}")
        try:
            return resolved.read_bytes()
        except OSError as error:
            raise SkillUnknownError(f"附件不存在：{relative_path}") from error

    # ------------------------------------------------------------------ 写入

    def commit_writes(self, writes: list[tuple[str, bytes | None]], message: str) -> None:
        """一次临界区内完成若干文件的写入/删除并提交；任一步失败恢复原状。

        `writes` 的路径相对实例数据目录（如 `skills/<id>/SKILL.md`），`None` 表示删除。
        """

        self.initialize()
        with self._lock, self._cross_process_lock():
            backups: list[tuple[Path, bytes | None]] = []
            relatives: list[str] = []
            try:
                for relative, content in writes:
                    path = self.data_dir / relative
                    backups.append((path, path.read_bytes() if path.exists() else None))
                    relatives.append(relative)
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._atomic_write(path, content)
                self._git("add", "-A", "--", *relatives)
                self._git("commit", "--quiet", "--only", "-m", message, "--", *relatives)
            except BaseException:
                self._restore(backups)
                raise

    def commit_skill(
        self, skill: Skill, message: str, attachments: dict[str, bytes] | None = None
    ) -> None:
        """保存整个技能目录：正文加（可选）若干附件，一次提交。"""

        writes: list[tuple[str, bytes | None]] = [
            (f"{skill_dir_path(skill.skill_id)}/{SKILL_FILE}", render_skill_md(skill))
        ]
        for relative, content in (attachments or {}).items():
            writes.append((f"{skill_dir_path(skill.skill_id)}/{relative}", content))
        self.commit_writes(writes, message)

    # ------------------------------------------------------------------ 版本历史

    def versions(self, skill_id: str) -> list[SkillVersion]:
        """从 Git log 派生版本列表；每条历史内容重算一次内容版本。"""

        self.initialize()
        scope = skill_dir_path(skill_id)
        # 记录间用 NUL 分隔：提交说明本身含换行，不能按行切。
        result = self._git_bytes("log", "--format=%H%x01%aI%x01%B%x00", "--", scope)
        if result.returncode != 0:
            raise SkillStoreUnavailableError("无法读取技能版本历史")
        versions: list[SkillVersion] = []
        for record in result.stdout.split(b"\x00"):
            if not record.strip():
                continue
            # 记录间以换行相隔：剥掉前导空白后 hash 首位即是记录起点。
            commit, created_at, message = record.strip().decode("utf-8").split("\x01", 2)
            skill = self._snapshot_at(skill_id, commit)
            if skill is None:
                continue
            trailers = dict(_TRAILER.findall(message))
            versions.append(
                SkillVersion(
                    commit=commit,
                    revision=skill.revision,
                    created_at=created_at,
                    change_id=trailers.get("change-id"),
                    actor=trailers.get("actor"),
                    reason=trailers.get("reason"),
                    skill=skill,
                )
            )
        return versions

    def version_snapshot(self, skill_id: str, commit: str) -> Skill:
        skill = self._snapshot_at(skill_id, commit)
        if skill is None:
            raise SkillUnknownError(f"版本不存在：{skill_id}@{commit[:12]}")
        return skill

    def _snapshot_at(self, skill_id: str, commit: str) -> Skill | None:
        scope = skill_dir_path(skill_id)
        listing = self._git_raw("ls-tree", "-r", "--name-only", commit, "--", scope)
        if listing.returncode != 0:
            return None
        raw: str | None = None
        files: list[SkillFile] = []
        for line in listing.stdout.splitlines():
            if not line:
                continue
            shown = self._git_bytes("show", f"{commit}:{line}")
            if shown.returncode != 0:
                continue
            if line.endswith(f"/{SKILL_FILE}"):
                raw = shown.stdout.decode("utf-8")
            else:
                files.append(
                    SkillFile(
                        relative_path=line[len(scope) + 1 :],
                        content_hash=content_hash(shown.stdout),
                    )
                )
        if raw is None:
            return None
        try:
            return self._build_skill(skill_id, raw, files)
        except ValueError as error:
            logger.warning("技能 %s 在提交 %s 的内容不合法：%s", skill_id, commit[:12], error)
            return None

    # ------------------------------------------------------------------ 内部

    def initialize(self) -> None:
        if self._ready:
            return
        try:
            self.skills_root.mkdir(parents=True, exist_ok=True)
            if not (self.data_dir / ".git").exists():
                self._git("init", "--quiet")
            root = self._git("rev-parse", "--show-toplevel").stdout.strip()
            if Path(root).resolve() != self.data_dir:
                raise SkillStoreUnavailableError("实例数据目录不是独立的 Git 仓库")
            self._git("config", "user.name", "Pebble")
            self._git("config", "user.email", "pebble@local")
            ignore = self.data_dir / ".gitignore"
            if not ignore.exists() or ignore.read_text(encoding="utf-8") != GITIGNORE:
                self._atomic_write(ignore, GITIGNORE.encode("utf-8"))
            self._ready = True
        except (OSError, UnicodeError) as error:
            raise SkillStoreUnavailableError("无法初始化技能目录") from error

    def _build_skill(
        self, skill_id: str, raw: str, files: list[SkillFile] | tuple[SkillFile, ...]
    ) -> Skill:
        frontmatter, body = parse_skill_md(raw)
        try:
            origin = SkillOrigin(frontmatter["origin"])
            state = SkillState(frontmatter["state"])
        except ValueError as error:
            raise ValueError(f"frontmatter 枚举值不合法：{error}") from error
        return Skill(
            skill_id=skill_id,
            name=str(frontmatter["name"]),
            description=str(frontmatter["description"]),
            origin=origin,
            managed=bool(frontmatter["managed"]),
            state=state,
            created_at=str(frontmatter.get("created_at", "")),
            updated_at=str(frontmatter.get("updated_at", "")),
            body=body,
            files=tuple(sorted(files, key=lambda item: item.relative_path)),
        )

    def _scan_files(self, skill_id: str) -> list[SkillFile]:
        root = self.skills_root / skill_id
        files: list[SkillFile] = []
        if not root.is_dir():
            return files
        for prefix in ATTACHMENT_PREFIXES:
            base = root / prefix
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    files.append(
                        SkillFile(
                            relative_path=f"{prefix}{path.relative_to(base).as_posix()}",
                            content_hash=content_hash(path.read_bytes()),
                        )
                    )
        return files

    def _dirty_skill_ids(self) -> set[str]:
        """工作区相对 Git 有未提交改动的技能：直接改磁盘不产生变更记录，退出目录。"""

        status = self._git_raw("status", "--porcelain", "--untracked-files=all", "--", SKILLS_DIR)
        if status.returncode != 0:
            raise SkillStoreUnavailableError("无法检查技能目录改动")
        dirty: set[str] = set()
        for line in status.stdout.splitlines():
            if not line:
                continue
            parts = line[3:].strip('"').split("/")
            if len(parts) >= 2 and parts[0] == SKILLS_DIR:
                dirty.add(parts[1])
        return dirty

    def _restore(self, backups: list[tuple[Path, bytes | None]]) -> None:
        for path, previous in backups:
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    self._atomic_write(path, previous)
                self._git_raw("reset", "--quiet", "--", path.relative_to(self.data_dir).as_posix())
            except OSError:
                logger.exception("技能写入失败后无法恢复 %s", path)

    @contextmanager
    def _cross_process_lock(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.data_dir / ".skills.lock", "w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._git_raw(*args)
        if result.returncode != 0:
            raise SkillStoreUnavailableError("无法更新技能版本历史")
        return result

    def _git_raw(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(self.data_dir), *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise SkillStoreUnavailableError("本机 Git 不可用") from error

    def _git_bytes(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "-C", str(self.data_dir), *args],
                capture_output=True,
                check=False,
            )
        except OSError as error:
            raise SkillStoreUnavailableError("本机 Git 不可用") from error
