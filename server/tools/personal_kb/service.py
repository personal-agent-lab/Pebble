"""Markdown 个人资料库：文件落盘 + 数据目录内本地 Git 版本历史。

第一阶段只做保存、读取（含历史版本）与修改：资料的稳定身份写在 frontmatter，版本即
Git 提交，与 `memory/` 共用同一个数据目录仓库与同一把进程锁。检索索引、文件变更自动
同步、移动/重命名/删除与管理界面留待后续阶段。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import yaml

from server.errors import (
    KbStoreUnavailableError,
    KbValidationError,
    NotFoundError,
    VersionConflictError,
)
from server.sessions.service import timestamp
from server.storage.datarepo import GITIGNORE, lock_for

ID_PREFIX = "kb_"
DEFAULT_DIR = "inbox"
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
FIELD_ORDER = ("id", "title", "tags", "source", "created_at", "updated_at")


class KbStore:
    """读写 `kb/` 下的 Markdown 资料；每次调用都重新读取磁盘，不缓存内容。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.kb_dir = self.data_dir / "kb"
        self._lock = lock_for(self.data_dir)
        with self._lock:
            self._initialize()

    # ------------------------------------------------------------------ 公开操作

    def save(
        self,
        *,
        title: str,
        body: str,
        path: str | None = None,
        tags: list[str] | None = None,
        source: dict | None = None,
    ) -> dict:
        """新建一份资料文件并提交；返回 id、相对路径、版本（commit）与引用。"""
        errors = []
        if not (title or "").strip():
            errors.append({"field": "title", "message": "资料标题不能为空"})
        if not (body or "").strip():
            errors.append({"field": "body", "message": "资料正文不能为空"})
        if errors:
            raise KbValidationError(errors)

        doc_id = f"{ID_PREFIX}{uuid4().hex}"
        with self._lock:
            rel = self._normalize_rel(path) if path else self._default_rel(title, doc_id)
            target = self.data_dir / rel
            if target.exists():
                raise KbValidationError(
                    [{"field": "path", "message": "目标文件已存在，请改用 kb_update 修改"}]
                )
            now = timestamp()
            meta = self._build_meta(doc_id, title.strip(), now, now, tags, source)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._atomic_write(target, self._render(meta, body))
                commit = self._commit(rel, f"[Kb] Add {meta['title']}")
            except Exception as error:
                self._discard_new(target, rel)
                if isinstance(error, KbStoreUnavailableError):
                    raise
                raise KbStoreUnavailableError("资料保存失败，已撤销") from error
            return self._result(doc_id, rel, meta, commit)

    def read(
        self, *, path: str | None = None, doc_id: str | None = None, version: str | None = None
    ) -> dict:
        """读取资料原文；指定 version（commit）时返回该历史版本，不总结。"""
        with self._lock:
            rel = self._locate(path, doc_id)
            raw = self._content_at(rel, version)
            meta, body = self._parse(raw)
            commit = version or self._file_commit(rel)
            doc_id = meta.get("id") or doc_id
            return {
                "id": doc_id,
                "title": meta.get("title"),
                "tags": meta.get("tags"),
                "source": meta.get("source"),
                "body": body,
                "ref": {
                    "id": doc_id,
                    "path": rel,
                    "commit": commit,
                    "version": meta.get("updated_at"),
                },
            }

    def list(self, *, directory: str | None = None) -> dict:
        """列出资料及其当前位置；用于尚不知道 path/id 时发现资料。"""
        with self._lock:
            prefix = self._normalize_directory(directory)
            documents = []
            for file in sorted((self.data_dir / prefix).rglob("*.md")):
                try:
                    meta, _ = self._parse(file.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, KbStoreUnavailableError):
                    continue
                rel = self._relative(file)
                documents.append(
                    {
                        "id": meta.get("id"),
                        "path": rel,
                        "title": meta.get("title"),
                        "tags": meta.get("tags"),
                        "version": self._file_commit(rel),
                    }
                )
            return {"directory": prefix, "documents": documents}

    def history(self, *, path: str | None = None, doc_id: str | None = None) -> dict:
        """列出一份资料的已有版本，供 `read(version=...)` 读取历史原文。"""
        with self._lock:
            rel = self._locate(path, doc_id)
            meta, _ = self._parse(self._content_at(rel, None))
            result = self._git(
                "log", "--format=%H%x00%cI%x00%s", "--", rel
            ).stdout.splitlines()
            versions = []
            for line in result:
                commit, changed_at, summary = line.split("\0", 2)
                versions.append(
                    {"version": commit, "changed_at": changed_at, "summary": summary}
                )
            return {
                "id": meta.get("id") or doc_id,
                "path": rel,
                "title": meta.get("title"),
                "versions": versions,
            }

    def update(
        self,
        *,
        expected_version: str,
        path: str | None = None,
        doc_id: str | None = None,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        source: dict | None = None,
    ) -> dict:
        """修改已有资料而非新建副本；版本不匹配则拒绝，不静默覆盖。"""
        with self._lock:
            rel = self._locate(path, doc_id)
            current = self._file_commit(rel)
            if expected_version != current:
                raise VersionConflictError(current)
            meta, old_body = self._parse(self._content_at(rel, None))
            if title is not None:
                if not title.strip():
                    raise KbValidationError([{"field": "title", "message": "资料标题不能为空"}])
                meta["title"] = title.strip()
            if body is not None:
                if not body.strip():
                    raise KbValidationError([{"field": "body", "message": "资料正文不能为空"}])
                old_body = body
            if tags is not None:
                if tags:
                    meta["tags"] = list(tags)
                else:
                    meta.pop("tags", None)
            if source is not None:
                if source:
                    meta["source"] = source
                else:
                    meta.pop("source", None)
            meta["updated_at"] = timestamp()

            target = self.data_dir / rel
            previous = target.read_bytes()
            try:
                self._atomic_write(target, self._render(self._order(meta), old_body))
                commit = self._commit(rel, f"[Kb] Update {meta.get('title', rel)}")
            except Exception as error:
                self._restore(target, previous)
                if isinstance(error, KbStoreUnavailableError):
                    raise
                raise KbStoreUnavailableError("资料修改失败，文件已恢复") from error
            return self._result(
                meta.get("id") or doc_id,
                rel,
                meta,
                commit,
                previous_version=current,
            )

    # ------------------------------------------------------------------ 初始化

    def _initialize(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.kb_dir / DEFAULT_DIR).mkdir(parents=True, exist_ok=True)
            self._ensure_repository()
            ignore = self.data_dir / ".gitignore"
            if not ignore.exists() or ignore.read_text(encoding="utf-8") != GITIGNORE:
                self._atomic_write(ignore, GITIGNORE)
        except KbStoreUnavailableError:
            raise
        except (OSError, UnicodeError) as error:
            raise KbStoreUnavailableError("无法初始化资料库目录") from error

    def _ensure_repository(self) -> None:
        if not (self.data_dir / ".git").exists():
            self._git("init", "--quiet")
        root = self._git("rev-parse", "--show-toplevel").stdout.strip()
        if Path(root).resolve() != self.data_dir:
            raise KbStoreUnavailableError("实例数据目录不是独立的 Git 仓库")
        self._git("config", "user.name", "Pebble")
        self._git("config", "user.email", "pebble@local")

    # ------------------------------------------------------------------ 路径与定位

    def _normalize_rel(self, path: str) -> str:
        """把模型给的相对路径规范化为 `kb/...md`；拒绝绝对路径与越界段。"""
        cleaned = (path or "").strip().replace("\\", "/")
        if not cleaned or cleaned.startswith("/"):
            raise KbValidationError([{"field": "path", "message": "path 必须是资料库内的相对路径"}])
        if cleaned == "kb" or cleaned.startswith("kb/"):
            cleaned = cleaned[len("kb/") :]
        parts = cleaned.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise KbValidationError([{"field": "path", "message": "path 不能包含空目录段或 .."}])
        if not parts[-1].endswith(".md"):
            parts[-1] += ".md"
        return "kb/" + "/".join(parts)

    def _normalize_directory(self, directory: str | None) -> str:
        if directory is None or not directory.strip() or directory.strip() == "kb":
            return "kb"
        cleaned = directory.strip().replace("\\", "/")
        if cleaned.startswith("kb/"):
            cleaned = cleaned[3:]
        parts = cleaned.strip("/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise KbValidationError(
                [{"field": "directory", "message": "目录必须位于资料库内"}]
            )
        return "kb/" + "/".join(parts)

    def _default_rel(self, title: str, doc_id: str) -> str:
        return f"kb/{DEFAULT_DIR}/{self._slug(title)}-{doc_id[-6:]}.md"

    @staticmethod
    def _slug(title: str) -> str:
        slug = re.sub(r"[\s/\\:*?\"<>|]+", "-", title.strip()).strip("-")
        return (slug or "note")[:40]

    def _locate(self, path: str | None, doc_id: str | None) -> str:
        if path:
            rel = self._normalize_rel(path)
            if not (self.data_dir / rel).exists():
                raise NotFoundError(rel)
            return rel
        if doc_id:
            rel = self._find_by_id(doc_id)
            if rel is None:
                raise NotFoundError(doc_id)
            return rel
        raise KbValidationError([{"field": "path", "message": "必须提供 path 或 id"}])

    def _find_by_id(self, doc_id: str) -> str | None:
        for file in sorted(self.kb_dir.rglob("*.md")):
            try:
                meta, _ = self._parse(file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, KbStoreUnavailableError):
                continue
            if meta.get("id") == doc_id:
                return self._relative(file)
        return None

    # ------------------------------------------------------------------ frontmatter

    @staticmethod
    def _build_meta(
        doc_id: str,
        title: str,
        created_at: str,
        updated_at: str,
        tags: list[str] | None,
        source: dict | None,
    ) -> dict:
        meta: dict = {"id": doc_id, "title": title}
        if tags:
            meta["tags"] = list(tags)
        if source:
            meta["source"] = source
        meta["created_at"] = created_at
        meta["updated_at"] = updated_at
        return meta

    @staticmethod
    def _order(meta: dict) -> dict:
        ordered = {key: meta[key] for key in FIELD_ORDER if key in meta}
        for key, value in meta.items():
            ordered.setdefault(key, value)
        return ordered

    @staticmethod
    def _render(meta: dict, body: str) -> str:
        front = yaml.safe_dump(
            meta, allow_unicode=True, sort_keys=False, default_flow_style=False
        ).strip()
        return f"---\n{front}\n---\n\n{body.strip()}\n"

    @staticmethod
    def _parse(raw: str) -> tuple[dict, str]:
        match = FRONTMATTER_RE.match(raw.replace("\r\n", "\n"))
        if not match:
            return {}, raw.strip()
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as error:
            raise KbStoreUnavailableError("资料 frontmatter 解析失败") from error
        if not isinstance(meta, dict):
            raise KbStoreUnavailableError("资料 frontmatter 不是键值对")
        return meta, match.group(2).strip()

    # ------------------------------------------------------------------ 结果与版本

    @staticmethod
    def _result(
        doc_id: str | None,
        rel: str,
        meta: dict,
        commit: str,
        previous_version: str | None = None,
    ) -> dict:
        result = {
            "id": doc_id,
            "path": rel,
            "title": meta.get("title"),
            "version": commit,
            "ref": {
                "id": doc_id,
                "path": rel,
                "commit": commit,
                "version": meta.get("updated_at"),
            },
        }
        if previous_version is not None:
            result["previous_version"] = previous_version
        return result

    def _file_commit(self, rel: str) -> str:
        result = self._git_raw("log", "-1", "--format=%H", "--", rel)
        return result.stdout.strip() or self._head()

    def _content_at(self, rel: str, version: str | None) -> str:
        if version:
            result = self._git_raw("show", f"{version}:{rel}")
            if result.returncode != 0:
                raise NotFoundError(f"{rel}@{version}")
            return result.stdout
        try:
            return (self.data_dir / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise KbStoreUnavailableError(f"无法读取 {rel}") from error

    # ------------------------------------------------------------------ Git 与原子写

    def _commit(self, rel: str, message: str) -> str:
        self._git("add", "--", rel)
        self._git("commit", "--quiet", "--only", "-m", message, "--", rel)
        return self._head()

    def _restore(self, path: Path, previous: bytes) -> None:
        try:
            self._atomic_write_bytes(path, previous)
            self._git("add", "--", self._relative(path))
        except Exception as rollback_error:
            raise KbStoreUnavailableError("资料修改失败，且无法恢复原文件") from rollback_error

    def _discard_new(self, path: Path, rel: str) -> None:
        try:
            if path.exists():
                path.unlink()
            self._git_raw("reset", "--quiet", "--", rel)
        except OSError:
            pass

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _relative(self, path: Path) -> str:
        return str(path.relative_to(self.data_dir)).replace(os.sep, "/")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._git_raw(*args)
        if result.returncode != 0:
            raise KbStoreUnavailableError("无法更新资料版本历史")
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
            raise KbStoreUnavailableError("本机 Git 不可用") from error

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        KbStore._atomic_write_bytes(path, content.encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
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
