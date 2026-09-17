"""Markdown 个人资料库：文件落盘 + 数据目录内本地 Git 版本历史 + 派生检索索引。

资料本体是 Markdown 文件，版本即 Git 提交，使用数据目录仓库，与 `memory/` 共用同一把
进程锁。检索走 `kb-index.sqlite3` 这份不进 Git 的派生索引：它只收录内容与 Git 版本一致的
文件，写入后在同一把锁内增量更新，缺失、损坏或与 `kb/` 目录状态不一致时整体重建。

用户可以直接在文件系统里改资料：每次公开操作开始前先把 `kb/` 下未提交的改动纳入版本
（`_intake`），移动按 frontmatter 的 `id` 识别为同一份资料并单独提交，保证历史能跟随路径
变化。删除只移除文件，版本历史保留，可按历史版本恢复。
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
from server.tools.personal_kb.catalog import CatalogEntry, render_catalog
from server.tools.personal_kb.index import (
    IndexDocument,
    KbIndex,
    normalize_terms,
    split_sections,
)

ID_PREFIX = "kb_"
DEFAULT_DIR = "inbox"
ARCHIVE_DIR = "archive"
INDEX_FILENAME = "kb-index.sqlite3"
MAX_RESULTS_DEFAULT = 10
MAX_RESULTS_LIMIT = 20
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
FIELD_ORDER = ("id", "title", "summary", "tags", "created_at", "updated_at")

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
        summary: str | None = None,
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
            self._intake()
            rel = self._normalize_rel(path) if path else self._default_rel(title, doc_id)
            target = self.data_dir / rel
            if target.exists():
                raise KbValidationError(
                    [{"field": "path", "message": "目标文件已存在，请改用 kb_update 修改"}]
                )
            now = timestamp()
            meta = self._build_meta(doc_id, title.strip(), now, now, tags, summary)
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
            index_status = self._sync_index([rel], tree_before)
            return self._result(doc_id, rel, meta, commit, rendered, index_status=index_status)

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
            self._intake()
            if ref is not None:
                return self._read_ref(ref)
            if version:
                commit = self._full_commit(version)
                if commit is None:
                    raise NotFoundError(f"{path or doc_id}@{version}")
                rel = self._path_at(self._resolve(path, doc_id)[0], commit)
                raw = self._content_at(rel, commit)
            else:
                rel = self._locate(path, doc_id)
                raw = self._content_at(rel, None)
                commit = self._file_commit(rel)
            meta, body = self._parse(raw)
            doc_id = meta.get("id") or doc_id
            lines = self._body_lines(meta.get("title") or "", raw)
            return {
                "id": doc_id,
                "path": rel,
                "title": meta.get("title"),
                "tags": meta.get("tags"),
                "summary": meta.get("summary"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
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
            self._intake()
            try:
                tree = self._kb_tree()
                if not self._index.is_current(tree):
                    self._index.rebuild(tree, self._documents())
            except Exception as error:
                # 索引跟不上资料时宁可说不可检索，也不拿可能过期的旧索引回答。
                raise KbIndexUnavailableError("资料索引不可用，无法检索") from error
            hits = self._index.search(terms=terms, tag=tag, max_results=limit)
        return {"query": query, "results": [self._hit_payload(hit) for hit in hits]}

    def list(self, *, directory: str | None = None, deleted: bool = False) -> dict:
        """列出资料及其当前位置；用于尚不知道 path/id 时发现资料。

        `deleted` 为真时改为列出已删除（且没有被恢复）的资料，附删除前最后的版本，供恢复。
        """
        with self._lock:
            self._intake()
            prefix = self._normalize_directory(directory)
            if deleted:
                return {"directory": prefix, "deleted": True, "documents": self._deleted(prefix)}
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
                        "summary": meta.get("summary"),
                        "tags": meta.get("tags"),
                        "version": self._file_commit(rel),
                    }
                )
            return {"directory": prefix, "documents": documents}

    def history(self, *, path: str | None = None, doc_id: str | None = None) -> dict:
        """列出一份资料的已有版本（跟随移动，含已删除资料），供读取或恢复历史原文。"""
        with self._lock:
            self._intake()
            rel, current = self._resolve(path, doc_id)
            entries = self._history_entries(rel, doc_id)
            exists = current is not None
            latest = next((entry for entry in entries if not entry["deleted"]), None)
            meta: dict = {}
            if latest is not None:
                meta, _ = self._parse(self._content_at(latest["path"], latest["version"]))
            return {
                "id": meta.get("id") or doc_id,
                "path": rel,
                "title": meta.get("title"),
                "deleted": not exists,
                "versions": entries,
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
        summary: str | None = None,
    ) -> dict:
        """修改已有资料而非新建副本；版本不匹配则拒绝，不静默覆盖。"""
        with self._lock:
            self._intake()
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
            if summary is not None:
                if summary.strip():
                    meta["summary"] = summary.strip()
                else:
                    meta.pop("summary", None)
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
            index_status = self._sync_index([rel], tree_before)
            return self._result(
                meta.get("id") or doc_id,
                rel,
                meta,
                commit,
                rendered,
                previous_version=current,
                index_status=index_status,
            )

    def catalog(self, limit: int | None = None) -> str:
        """每轮常驻的资料目录：先纳入用户改动，再从文件现算，保证与资料一致。"""
        with self._lock:
            self._intake()
            entries = []
            for file in self.kb_dir.rglob("*.md"):
                meta = self._meta_of(file)
                title = meta.get("title")
                summary = meta.get("summary")
                updated_at = meta.get("updated_at")
                if hasattr(updated_at, "isoformat"):
                    # 用户手写、未加引号的时间会被 YAML 解析成 datetime，统一回 ISO 文本再排序。
                    updated_at = updated_at.isoformat()
                named = isinstance(title, str) and title.strip()
                entries.append(
                    CatalogEntry(
                        path=self._relative(file),
                        title=title.strip() if named else file.stem,
                        summary=summary if isinstance(summary, str) else None,
                        updated_at=updated_at if isinstance(updated_at, str) else "",
                    )
                )
            return render_catalog(entries) if limit is None else render_catalog(entries, limit)

    def archive(self, *, title: str, items: list[dict]) -> dict:
        """把一次任务的关键信息与逐项结果归档到 `kb/archive/`：原始内容与总结分节保存。"""
        errors = []
        if not (title or "").strip():
            errors.append({"field": "title", "message": "归档标题不能为空"})
        if not isinstance(items, list) or not items:
            errors.append({"field": "items", "message": "归档至少需要一项内容"})
            items = []
        sections = {"original": [], "summary": []}
        for position, item in enumerate(items):
            kind = item.get("kind") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if kind not in sections:
                errors.append(
                    {
                        "field": f"items[{position}].kind",
                        "message": "kind 必须是 original 或 summary",
                    }
                )
                continue
            if not isinstance(text, str) or not text.strip():
                errors.append({"field": f"items[{position}].text", "message": "内容不能为空"})
                continue
            heading = item.get("heading")
            label = heading.strip() if isinstance(heading, str) and heading.strip() else None
            sections[kind].append((label, text.strip()))
        if errors:
            raise KbValidationError(errors)
        parts = []
        for kind, name in (("original", "原始内容"), ("summary", "总结与执行结果")):
            for label, text in sections[kind]:
                parts.append(f"## {name}：{label}\n\n{text}" if label else f"## {name}\n\n{text}")
        doc_id_hint = uuid4().hex[-6:]
        date = timestamp()[:10]
        path = f"{ARCHIVE_DIR}/{date}-{self._slug(title)}-{doc_id_hint}.md"
        return self.save(title=title.strip(), body="\n\n".join(parts), path=path)

    def delete(
        self,
        *,
        expected_version: str,
        path: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        """删除资料文件并提交；历史版本保留，可用 `restore` 找回。"""
        with self._lock:
            self._intake()
            rel = self._locate(path, doc_id)
            current = self._file_commit(rel)
            if expected_version != current:
                raise VersionConflictError(current)
            meta, _ = self._parse(self._content_at(rel, None))
            tree_before = self._kb_tree()
            title = meta.get("title") or rel
            self._git("rm", "--quiet", "--", rel)
            try:
                self._git("commit", "--quiet", "-m", f"[Kb] Delete {title}", "--", rel)
            except KbStoreUnavailableError:
                self._git_raw("reset", "--quiet", "HEAD", "--", rel)
                self._git_raw("checkout", "--", rel)
                raise
            index_status = self._sync_index([rel], tree_before)
            return {
                "id": meta.get("id") or doc_id,
                "path": rel,
                "title": meta.get("title"),
                "previous_version": current,
                "version": self._head(),
                "index_status": index_status,
            }

    def move(
        self,
        *,
        expected_version: str,
        new_path: str,
        path: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        """移动或重命名资料；内容与 `id` 不变，历史随路径跟随。"""
        with self._lock:
            self._intake()
            rel = self._locate(path, doc_id)
            current = self._file_commit(rel)
            if expected_version != current:
                raise VersionConflictError(current)
            new_rel = self._normalize_rel(new_path)
            if new_rel == rel:
                raise KbValidationError([{"field": "new_path", "message": "新位置与当前位置相同"}])
            target = self.data_dir / new_rel
            if target.exists():
                raise KbValidationError([{"field": "new_path", "message": "目标位置已有资料"}])
            meta, _ = self._parse(self._content_at(rel, None))
            tree_before = self._kb_tree()
            target.parent.mkdir(parents=True, exist_ok=True)
            self._git("mv", "--", rel, new_rel)
            title = meta.get("title") or new_rel
            try:
                self._git(
                    "commit", "--quiet", "-m", f"[Kb] Move {title}", "--", rel, new_rel
                )
            except KbStoreUnavailableError:
                self._git_raw("mv", "--", new_rel, rel)
                raise
            index_status = self._sync_index([rel, new_rel], tree_before)
            return {
                "id": meta.get("id") or doc_id,
                "title": meta.get("title"),
                "previous_path": rel,
                "path": new_rel,
                "previous_version": current,
                "version": self._head(),
                "index_status": index_status,
            }

    def restore(
        self, *, version: str, path: str | None = None, doc_id: str | None = None
    ) -> dict:
        """把资料恢复为某个历史版本；已删除的资料在该版本所在路径重建。恢复产生新提交。"""
        with self._lock:
            self._intake()
            rel, current = self._resolve(path, doc_id)
            commit = self._full_commit(version)
            entries = self._history_entries(rel, doc_id)
            entry = next((item for item in entries if item["version"] == commit), None)
            if commit is None or entry is None:
                raise NotFoundError(f"{path or doc_id}@{version}")
            if entry["deleted"]:
                raise KbValidationError(
                    [{"field": "version", "message": "该版本是删除记录，请选择删除之前的版本"}]
                )
            raw = self._content_at(entry["path"], commit)
            meta, body = self._parse(raw)
            exists = current is not None
            target_rel = rel if exists else entry["path"]
            target = self.data_dir / target_rel
            if not exists and target.exists():
                raise KbValidationError(
                    [{"field": "path", "message": f"原位置 {target_rel} 已被其他资料占用"}]
                )
            previous_version = self._file_commit(rel) if exists else None
            previous = target.read_bytes() if exists else None
            meta["updated_at"] = timestamp()
            tree_before = self._kb_tree()
            target.parent.mkdir(parents=True, exist_ok=True)
            title = meta.get("title") or target_rel
            try:
                rendered = self._render(self._order(meta), body)
                self._atomic_write(target, rendered)
                new_commit = self._commit(target_rel, f"[Kb] Restore {title}")
            except Exception as error:
                if previous is not None:
                    self._restore(target, previous)
                else:
                    self._discard_new(target, target_rel)
                if isinstance(error, KbStoreUnavailableError):
                    raise
                raise KbStoreUnavailableError("资料恢复失败，已撤销") from error
            index_status = self._sync_index([target_rel], tree_before)
            result = self._result(
                meta.get("id") or doc_id,
                target_rel,
                meta,
                new_commit,
                rendered,
                previous_version=previous_version,
                index_status=index_status,
            )
            result["restored_from"] = commit
            return result

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

    def _sync_index(self, paths: list[str], tree_before: str) -> str:
        """写入后把索引对齐到当前版本：只替换受影响的资料，索引本来就不完整时整体重建。

        索引是派生数据：失败不回滚已提交的资料，返回 stale 由下一次检索重建。
        """
        try:
            tree = self._kb_tree()
            if self._index.is_current(tree_before):
                for rel in paths:
                    document = self._document(rel)
                    if document is not None:
                        self._index.replace(document, tree=tree)
                    else:
                        self._index.drop(rel, tree=tree)
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

    # ------------------------------------------------------------------ 用户改动的跟随

    def _intake(self) -> None:
        """把用户在 `kb/` 下直接做的改动纳入版本：补资料标识、识别移动，再提交。

        移动（旧路径删除、新路径出现且 `id` 相同）先以原内容单独提交一次，使 Git 的
        重命名追踪不受同时发生的内容修改影响；其余改动随后作为一次“用户编辑”提交。
        """
        status = self._git_raw("status", "--porcelain", "--untracked-files=all", "--", "kb")
        if status.returncode != 0:
            raise KbStoreUnavailableError("无法检查资料库改动")
        if not status.stdout.strip():
            return
        try:
            self._git("add", "--all", "--", "kb")
            changes = self._staged_changes()
            known = self._head_ids()
            deleted = {rel: known[rel] for rel, kind in changes if kind == "D" and rel in known}
            present_ids = {
                doc_id for rel, doc_id in known.items() if (self.data_dir / rel).exists()
            }
            moves = []
            for rel, kind in changes:
                if kind == "D" or not rel.endswith(".md"):
                    continue
                doc_id = self._ensure_identity(rel, kind == "A", present_ids)
                if kind == "A" and doc_id is not None:
                    origin = next(
                        (old for old, old_id in deleted.items() if old_id == doc_id), None
                    )
                    if origin is not None:
                        moves.append((origin, rel))
                        del deleted[origin]
            self._git("reset", "--quiet", "--", "kb")
            if moves:
                for origin, rel in moves:
                    blob = self._git("rev-parse", f"HEAD:{origin}").stdout.strip()
                    self._git("rm", "--cached", "--quiet", "--", origin)
                    self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}")
                self._git("commit", "--quiet", "-m", f"[Kb] User move {len(moves)} file(s)")
            self._git("add", "--all", "--", "kb")
            if self._git_raw("diff", "--cached", "--quiet", "--", "kb").returncode != 0:
                count = len(self._staged_changes())
                self._git("commit", "--quiet", "-m", f"[Kb] User edit {count} file(s)")
        except KbStoreUnavailableError:
            self._git_raw("reset", "--quiet", "--", "kb")
            raise
        except (OSError, UnicodeError) as error:
            self._git_raw("reset", "--quiet", "--", "kb")
            raise KbStoreUnavailableError("无法纳入资料库改动") from error

    def _staged_changes(self) -> list[tuple[str, str]]:
        """暂存区里 `kb/` 的逐文件改动（A/M/D），不合并重命名。"""
        result = self._git(
            "diff", "--cached", "--name-status", "--no-renames", "-z", "--", "kb"
        ).stdout
        parts = [part for part in result.split("\0") if part]
        return [(parts[index + 1], parts[index][0]) for index in range(0, len(parts) - 1, 2)]

    def _head_ids(self) -> dict[str, str]:
        """已提交版本里每份资料的 `id`，用于识别移动与复制。"""
        listing = self._git_raw("ls-tree", "-r", "--name-only", "-z", "HEAD", "--", "kb")
        ids = {}
        for rel in (item for item in listing.stdout.split("\0") if item.endswith(".md")):
            shown = self._git_raw("show", f"HEAD:{rel}")
            try:
                meta, _ = self._parse(shown.stdout)
            except KbStoreUnavailableError:
                continue
            if isinstance(meta.get("id"), str):
                ids[rel] = meta["id"]
        return ids

    def _ensure_identity(self, rel: str, added: bool, present_ids: set[str]) -> str | None:
        """给缺少标识的资料补上 frontmatter；复制出来的文件换一个新 `id`。正文不改动。"""
        target = self.data_dir / rel
        try:
            raw = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        normalized = raw.replace("\r\n", "\n")
        match = FRONTMATTER_RE.match(normalized)
        try:
            meta = (yaml.safe_load(match.group(1)) or {}) if match else {}
        except yaml.YAMLError:
            return None
        if not isinstance(meta, dict):
            return None
        doc_id = meta.get("id")
        copied = added and isinstance(doc_id, str) and doc_id in present_ids
        if isinstance(doc_id, str) and doc_id and not copied:
            return doc_id
        now = timestamp()
        meta["id"] = f"{ID_PREFIX}{uuid4().hex}"
        meta.setdefault("title", self._title_from(normalized, target.stem))
        meta.setdefault("created_at", now)
        meta.setdefault("updated_at", now)
        front = yaml.safe_dump(
            self._order(meta), allow_unicode=True, sort_keys=False, default_flow_style=False
        ).strip()
        body = match.group(2) if match else normalized
        separator = "" if match else "\n"
        self._atomic_write(target, f"---\n{front}\n---\n{separator}{body}")
        return meta["id"]

    @staticmethod
    def _title_from(text: str, fallback: str) -> str:
        for line in text.split("\n"):
            if line.startswith("# "):
                return line[2:].strip() or fallback
        return fallback

    def _history_entries(self, rel: str, doc_id: str | None = None) -> list[dict]:
        """一份资料从新到旧的版本：跟随重命名，删除记录标明 `deleted`。

        同一路径先后放过不同资料时，给出 `doc_id` 只保留这份资料自己的版本。
        """
        result = self._git(
            "-c",
            "core.quotepath=false",
            "log",
            "--follow",
            "--name-status",
            "--format=%x01%H%x00%cI%x00%s",
            "--",
            rel,
        ).stdout
        entries = []
        for block in result.split("\x01")[1:]:
            header, _, rest = block.partition("\n")
            commit, changed_at, summary = header.split("\0", 2)
            status_line = next((line for line in rest.splitlines() if line.strip()), "")
            fields = status_line.split("\t")
            kind = fields[0][:1] if fields else ""
            entry_path = fields[-1] if len(fields) > 1 else rel
            if doc_id is not None:
                probe = f"{commit}^:{entry_path}" if kind == "D" else f"{commit}:{entry_path}"
                try:
                    meta, _ = self._parse(self._git_raw("show", probe).stdout)
                except KbStoreUnavailableError:
                    continue
                if meta.get("id") != doc_id:
                    continue
            entries.append(
                {
                    "version": commit,
                    "changed_at": changed_at,
                    "summary": summary,
                    "path": entry_path,
                    "deleted": kind == "D",
                }
            )
        return entries

    def _path_at(self, rel: str, commit: str) -> str:
        """资料在某个版本时所在的路径：取该版本及之前最近一次改动时的路径。"""
        for entry in self._history_entries(rel):
            reachable = self._git_raw(
                "merge-base", "--is-ancestor", entry["version"], commit
            ).returncode
            if reachable == 0:
                if entry["deleted"]:
                    break
                return entry["path"]
        raise NotFoundError(f"{rel}@{commit}")

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

    def _resolve(self, path: str | None, doc_id: str | None) -> tuple[str, str | None]:
        """定位资料，包括已经删除的：返回（历史所在路径, 当前路径或 None）。

        按 id 定位时，只有文件里的 id 相同才算这份资料仍存在；原位置被别的资料占用不算。
        """
        if path:
            rel = self._normalize_rel(path)
            if (self.data_dir / rel).exists():
                return rel, rel
            if self._git_raw("log", "-1", "--format=%H", "--", rel).stdout.strip():
                return rel, None
            raise NotFoundError(rel)
        if doc_id:
            current = self._find_by_id(doc_id)
            if current is not None:
                return current, current
            deleted = self._find_deleted_by_id(doc_id)
            if deleted is None:
                raise NotFoundError(doc_id)
            return deleted, None
        raise KbValidationError([{"field": "path", "message": "必须提供 path 或 id"}])

    def _deleted(self, prefix: str) -> list[dict]:
        """已删除且当前不存在的资料，按删除时间从新到旧，每份资料只列最近一次删除。"""
        present = {
            meta.get("id")
            for meta in (self._meta_of(file) for file in self.kb_dir.rglob("*.md"))
            if meta
        }
        result = self._git_raw(
            "-c",
            "core.quotepath=false",
            "log",
            "--diff-filter=D",
            "--name-only",
            "--format=%x01%H%x00%cI",
            "--",
            prefix,
        ).stdout
        documents = []
        seen: set[str] = set()
        for block in result.split("\x01")[1:]:
            header, _, rest = block.partition("\n")
            commit, deleted_at = header.split("\0", 1)
            for rel in (line.strip() for line in rest.splitlines()):
                if not rel.endswith(".md"):
                    continue
                try:
                    meta, _ = self._parse(self._git_raw("show", f"{commit}^:{rel}").stdout)
                except KbStoreUnavailableError:
                    continue
                doc_id = meta.get("id")
                if not isinstance(doc_id, str) or doc_id in present or doc_id in seen:
                    continue
                seen.add(doc_id)
                last = self._git("log", "-1", "--format=%H", f"{commit}^", "--", rel).stdout
                documents.append(
                    {
                        "id": doc_id,
                        "path": rel,
                        "title": meta.get("title"),
                        "tags": meta.get("tags"),
                        "deleted_at": deleted_at.strip(),
                        "version": last.strip(),
                    }
                )
        return documents

    def _meta_of(self, file: Path) -> dict:
        try:
            meta, _ = self._parse(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, KbStoreUnavailableError):
            return {}
        return meta

    def _find_deleted_by_id(self, doc_id: str) -> str | None:
        """在删除记录里按 `id` 找资料最后所在的路径，取最近一次删除。"""
        result = self._git_raw(
            "-c",
            "core.quotepath=false",
            "log",
            "--diff-filter=D",
            "--name-only",
            "--format=%x01%H",
            "--",
            "kb",
        ).stdout
        for block in result.split("\x01")[1:]:
            commit, _, rest = block.partition("\n")
            for rel in (line.strip() for line in rest.splitlines()):
                if not rel.endswith(".md"):
                    continue
                shown = self._git_raw("show", f"{commit.strip()}^:{rel}")
                try:
                    meta, _ = self._parse(shown.stdout)
                except KbStoreUnavailableError:
                    continue
                if meta.get("id") == doc_id:
                    return rel
        return None

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
        summary: str | None = None,
    ) -> dict:
        meta: dict = {"id": doc_id, "title": title}
        if summary and summary.strip():
            meta["summary"] = summary.strip()
        if tags:
            meta["tags"] = list(tags)
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
            "ref": cls._ref(
                doc_id, rel, None, cls._body_lines(meta.get("title") or "", text), commit
            ),
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
        sections = split_sections(meta.get("title") or "", raw)
        section = next(
            (item for item in sections if item.start_line == start and item.end_line == end),
            None,
        )
        # 引用必须原样来自 kb_search / kb_read：合法区间只有真实分节与整篇正文区间。
        body_lines = [sections[0].start_line, sections[-1].end_line] if sections else [1, total]
        if section is None and lines != body_lines and lines != [1, total]:
            raise KbValidationError(
                [
                    {
                        "field": "ref.lines",
                        "message": "引用行号与资料分节不对应，请原样使用 kb_search 返回的 ref",
                    }
                ]
            )
        heading = section.heading if section is not None else None
        fragment = "\n".join(raw.replace("\r\n", "\n").split("\n")[start - 1 : end])
        doc_id = meta.get("id") or ref_id
        return {
            "id": doc_id,
            "title": meta.get("title"),
            "tags": meta.get("tags"),
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
        lines = raw.replace("\r\n", "\n").split("\n")
        # 结尾换行是最后一行的结束符，不产生新行；行号必须与编辑器里的真实行号一致。
        if lines and lines[-1] == "":
            lines.pop()
        return len(lines)

    @staticmethod
    def _body_lines(title: str, raw: str) -> list[int]:
        """正文（不含 frontmatter）的起止行号；返回正文要与行号严格对应。"""
        sections = split_sections(title, raw)
        if sections:
            return [sections[0].start_line, sections[-1].end_line]
        return [1, KbStore._line_count(raw)]

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
