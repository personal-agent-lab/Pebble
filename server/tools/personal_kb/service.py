"""Markdown 个人资料库：文件落盘 + 数据目录内本地 Git 版本历史 + 派生检索索引。

资料本体是 Markdown 文件，版本即 Git 提交，与 `memory/` 共用同一个数据目录仓库与同一把
进程锁。检索走 `kb-index.sqlite3` 这份不进 Git 的派生索引：它只收录内容与 Git 版本一致的
文件，写入后在同一把锁内增量更新，缺失、损坏或与 `kb/` 目录状态不一致时整体重建。文件
变更自动跟随、移动/重命名/删除与管理界面留待后续阶段。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import yaml

from server.errors import (
    KbIndexUnavailableError,
    KbStoreUnavailableError,
    KbValidationError,
    NotFoundError,
    VersionConflictError,
)
from server.sessions.service import timestamp
from server.storage.datarepo import GITIGNORE, lock_for
from server.tools.personal_kb.index import (
    IndexDocument,
    KbIndex,
    normalize_terms,
    split_sections,
)

ID_PREFIX = "kb_"
DEFAULT_DIR = "inbox"
INDEX_FILENAME = "kb-index.sqlite3"
MAX_RESULTS_DEFAULT = 10
MAX_RESULTS_LIMIT = 20
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
FIELD_ORDER = ("id", "title", "tags", "source", "created_at", "updated_at")

logger = logging.getLogger(__name__)


class KbStore:
    """读写 `kb/` 下的 Markdown 资料；每次调用都重新读取磁盘，不缓存内容。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.kb_dir = self.data_dir / "kb"
        self._index = KbIndex(self.data_dir / INDEX_FILENAME)
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
            tree_before = self._kb_tree()
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                rendered = self._render(meta, body)
                self._atomic_write(target, rendered)
                commit = self._commit(rel, f"[Kb] Add {meta['title']}")
            except Exception as error:
                self._discard_new(target, rel)
                if isinstance(error, KbStoreUnavailableError):
                    raise
                raise KbStoreUnavailableError("资料保存失败，已撤销") from error
            index_status = self._sync_index(rel, tree_before)
            return self._result(
                doc_id, rel, meta, commit, rendered, index_status=index_status
            )

    def read(
        self,
        *,
        path: str | None = None,
        doc_id: str | None = None,
        version: str | None = None,
        ref: dict | None = None,
    ) -> dict:
        """读取资料原文；指定 version（commit）时返回该历史版本，不总结。

        给 `ref` 时按引用里的 commit、路径与行号读取命中片段，而不是读整篇资料。
        """
        with self._lock:
            if ref is not None:
                return self._read_ref(ref)
            rel = self._locate(path, doc_id)
            raw = self._content_at(rel, version)
            meta, body = self._parse(raw)
            commit = self._full_commit(version) if version else self._file_commit(rel)
            doc_id = meta.get("id") or doc_id
            lines = [1, self._line_count(raw)]
            return {
                "id": doc_id,
                "title": meta.get("title"),
                "tags": meta.get("tags"),
                "source": meta.get("source"),
                "heading": None,
                "lines": lines,
                "commit": commit,
                "body": body,
                "ref": self._ref(doc_id, rel, None, lines, commit),
            }

    def search(
        self,
        *,
        query: str,
        source_kind: str | None = None,
        tag: str | None = None,
        max_results: int | None = None,
    ) -> dict:
        """按关键词检索分节；返回摘要与引用，不返回整篇正文。"""
        terms = normalize_terms(query)
        if not terms:
            raise KbValidationError([{"field": "query", "message": "检索词不能为空"}])
        limit = MAX_RESULTS_DEFAULT if max_results is None else max_results
        if not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS_LIMIT:
            raise KbValidationError(
                [
                    {
                        "field": "max_results",
                        "message": f"max_results 必须在 1–{MAX_RESULTS_LIMIT} 之间",
                    }
                ]
            )
        with self._lock:
            try:
                tree = self._kb_tree()
                if not self._index.is_current(tree):
                    self._index.rebuild(tree, self._documents())
            except Exception as error:
                # 索引跟不上资料时宁可说不可检索，也不拿可能过期的旧索引回答。
                raise KbIndexUnavailableError("资料索引不可用，无法检索") from error
            hits = self._index.search(
                terms=terms, source_kind=source_kind, tag=tag, max_results=limit
            )
        return {"query": query, "results": [self._hit_payload(hit) for hit in hits]}

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
            tree_before = self._kb_tree()
            try:
                rendered = self._render(self._order(meta), old_body)
                self._atomic_write(target, rendered)
                commit = self._commit(rel, f"[Kb] Update {meta.get('title', rel)}")
            except Exception as error:
                self._restore(target, previous)
                if isinstance(error, KbStoreUnavailableError):
                    raise
                raise KbStoreUnavailableError("资料修改失败，文件已恢复") from error
            index_status = self._sync_index(rel, tree_before)
            return self._result(
                meta.get("id") or doc_id,
                rel,
                meta,
                commit,
                rendered,
                previous_version=current,
                index_status=index_status,
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

    # ------------------------------------------------------------------ 索引同步

    def _sync_index(self, rel: str, tree_before: str) -> str:
        """写入后把索引对齐到当前版本：只替换这一份资料，索引本来就不完整时整体重建。

        索引是派生数据：失败不回滚已提交的资料，返回 stale 由下一次检索重建。
        """
        try:
            tree = self._kb_tree()
            if self._index.is_current(tree_before):
                document = self._document(rel)
                if document is not None:
                    self._index.replace(document, tree=tree)
                    return "ok"
            self._index.rebuild(tree, self._documents())
            return "ok"
        except Exception:
            logger.exception("资料索引同步失败，标记为待重建")
            return "stale"

    def _documents(self) -> list[IndexDocument]:
        documents = []
        for file in sorted(self.kb_dir.rglob("*.md")):
            document = self._document(self._relative(file))
            if document is not None:
                documents.append(document)
        return documents

    def _document(self, rel: str) -> IndexDocument | None:
        """只有磁盘内容与某个 Git 版本一致时才可索引；未纳入版本的内容不冒充已同步资料。"""
        try:
            raw = (self.data_dir / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        commit = self._file_commit(rel)
        snapshot = self._git_raw("show", f"{commit}:{rel}")
        if snapshot.returncode != 0 or snapshot.stdout != raw:
            return None
        try:
            meta, _ = self._parse(raw)
        except KbStoreUnavailableError:
            return None
        return IndexDocument(
            path=rel,
            meta=meta,
            sections=split_sections(meta.get("title") or "", raw),
            commit=commit,
        )

    def _kb_tree(self) -> str:
        """`kb/` 目录当前的 Git tree 标识；没有提交过资料时为空串。"""
        result = self._git_raw("rev-parse", "HEAD:kb")
        return result.stdout.strip() if result.returncode == 0 else ""

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
    def _ref(
        doc_id: str | None, rel: str, heading: str | None, lines: list[int], commit: str
    ) -> dict:
        """统一引用结构：commit 是唯一版本标识，行号可定位到具体分节。"""
        return {
            "id": doc_id,
            "path": rel,
            "heading": heading,
            "lines": list(lines),
            "commit": commit,
        }

    @classmethod
    def _result(
        cls,
        doc_id: str | None,
        rel: str,
        meta: dict,
        commit: str,
        text: str,
        previous_version: str | None = None,
        index_status: str = "ok",
    ) -> dict:
        result = {
            "id": doc_id,
            "path": rel,
            "title": meta.get("title"),
            "version": commit,
            "index_status": index_status,
            "ref": cls._ref(doc_id, rel, None, [1, cls._line_count(text)], commit),
        }
        if previous_version is not None:
            result["previous_version"] = previous_version
        return result

    @classmethod
    def _hit_payload(cls, hit) -> dict:
        lines = [hit.start_line, hit.end_line]
        return {
            "id": hit.doc_id,
            "path": hit.path,
            "title": hit.title,
            "heading": hit.heading,
            "lines": lines,
            "snippet": hit.snippet,
            "score": round(hit.score, 9),
            "ref": cls._ref(hit.doc_id, hit.path, hit.heading, lines, hit.commit),
        }

    def _read_ref(self, ref: dict) -> dict:
        """按引用读取历史原文片段：校验资料身份、路径与行号，越界或不匹配一律拒绝。"""
        if not isinstance(ref, dict):
            raise KbValidationError([{"field": "ref", "message": "ref 必须是引用对象"}])
        errors = []
        path_value = ref.get("path")
        commit_value = ref.get("commit")
        lines = ref.get("lines")
        if not isinstance(path_value, str) or not path_value.strip():
            errors.append({"field": "ref.path", "message": "引用缺少资料路径"})
        if not isinstance(commit_value, str) or not commit_value.strip():
            errors.append({"field": "ref.commit", "message": "引用缺少版本（commit）"})
        if not (
            isinstance(lines, list)
            and len(lines) == 2
            and all(isinstance(number, int) for number in lines)
        ):
            errors.append({"field": "ref.lines", "message": "引用缺少 [起始行, 结束行]"})
        if errors:
            raise KbValidationError(errors)

        rel = self._normalize_rel(path_value)
        commit = self._full_commit(commit_value)
        if commit is None:
            raise NotFoundError(f"{rel}@{commit_value}")
        raw = self._content_at(rel, commit)
        meta, _ = self._parse(raw)
        ref_id = ref.get("id")
        if ref_id and meta.get("id") != ref_id:
            raise KbValidationError([{"field": "ref.id", "message": "引用与资料身份不匹配"}])
        total = self._line_count(raw)
        start, end = lines
        if start < 1 or end < start or end > total:
            raise KbValidationError(
                [{"field": "ref.lines", "message": f"引用行号超出资料范围（1–{total}）"}]
            )
        fragment = "\n".join(raw.replace("\r\n", "\n").split("\n")[start - 1 : end])
        section = next(
            (
                item
                for item in split_sections(meta.get("title") or "", raw)
                if item.start_line == start and item.end_line == end
            ),
            None,
        )
        heading = section.heading if section is not None else ref.get("heading")
        if not isinstance(heading, str):
            heading = None
        doc_id = meta.get("id") or ref_id
        return {
            "id": doc_id,
            "title": meta.get("title"),
            "tags": meta.get("tags"),
            "source": meta.get("source"),
            "heading": heading,
            "lines": [start, end],
            "commit": commit,
            "body": fragment,
            "ref": self._ref(doc_id, rel, heading, [start, end], commit),
        }

    def _file_commit(self, rel: str) -> str:
        result = self._git_raw("log", "-1", "--format=%H", "--", rel)
        return result.stdout.strip() or self._head()

    def _full_commit(self, revision: str | None) -> str | None:
        """把模型给的版本写法规范化为完整 commit；不存在时返回 None。"""
        if not revision:
            return None
        result = self._git_raw("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _line_count(raw: str) -> int:
        return len(raw.replace("\r\n", "\n").split("\n"))

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
