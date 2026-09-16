"""Markdown 分节索引：按二级标题切分，用 SQLite FTS5 做关键词检索。

索引是派生数据，放在实例数据目录内、不进 Git，任何时刻都可以由 Markdown 文件与 Git 版本
重建。本模块只管 SQLite 一侧：分节切分、条目写入与替换、整体重建、检索与排序。扫描文件、
判定"磁盘内容与 Git 版本一致"、计算 `kb/` 的 Git tree 标识都在 `service.py`，那里持有数据
目录锁与 Git 边界。

行号一律是 Markdown 文件里 1-based 的真实行号，包含 frontmatter，便于按行号直接定位原文。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from server.db import session, write

# 索引自身的结构版本：与前端契约无关，改动索引字段或分节规则时递增，旧索引随即被重建。
INDEX_SCHEMA = 1

# trigram 分词器按三字符建立索引：1–2 个字的词无法用 MATCH 命中，改用同一张表的包含匹配。
FTS_MIN_TERM = 3

HEADING_RE = re.compile(r"^##(?!#)\s*(.*)$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

CREATE_SECTIONS = (
    "CREATE TABLE IF NOT EXISTS kb_sections ("
    "row_id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, path TEXT NOT NULL, title TEXT, "
    "source_kind TEXT, tags TEXT NOT NULL, heading TEXT NOT NULL, "
    "start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, commit_sha TEXT NOT NULL, "
    "content_hash TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS kb_sections_path ON kb_sections(path)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS kb_sections_fts USING fts5("
    "title, heading, tags, body, tokenize='trigram')",
    "CREATE TABLE IF NOT EXISTS kb_index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)

META_SCHEMA = "schema"
META_TREE = "tree"


@dataclass(frozen=True)
class Section:
    """一段可检索的分节；行号含 frontmatter，正文即该行的原文片段。"""

    heading: str
    start_line: int
    end_line: int
    body: str
    content_hash: str


@dataclass(frozen=True)
class IndexDocument:
    """一份可索引的资料：磁盘内容与 `commit` 版本一致，才允许以该版本进入索引。"""

    path: str
    meta: dict
    sections: tuple[Section, ...]
    commit: str


@dataclass(frozen=True)
class Hit:
    """一次检索命中的分节；`snippet` 只是摘要，回答依据仍需读取原文。"""

    doc_id: str
    path: str
    title: str | None
    source_kind: str | None
    tags: list[str]
    heading: str
    start_line: int
    end_line: int
    commit: str
    snippet: str
    score: float
    rank: int


def split_sections(title: str, text: str) -> tuple[Section, ...]:
    """按二级标题切分正文；无二级标题时整篇为一个分节，代码围栏里的伪标题不算。"""
    lines = text.replace("\r\n", "\n").split("\n")
    start = _body_start(lines)
    headings: list[tuple[int, str]] = []
    fenced = False
    for index in range(start, len(lines)):
        line = lines[index]
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING_RE.match(line)
        if match:
            headings.append((index, match.group(1).strip()))

    sections: list[Section] = []
    if not headings:
        return tuple(_section(title, lines, start, len(lines) - 1, None))
    sections.extend(_section(title, lines, start, headings[0][0] - 1, None))
    for position, (index, name) in enumerate(headings):
        end = headings[position + 1][0] - 1 if position + 1 < len(headings) else len(lines) - 1
        sections.extend(_section(title, lines, index, end, name))
    return tuple(sections)


def _body_start(lines: list[str]) -> int:
    """frontmatter 之后的第一行（0-based）；没有 frontmatter 时是文件开头。"""
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return 0


def _section(
    title: str, lines: list[str], start: int, end: int, name: str | None
) -> list[Section]:
    first, last = start, min(end, len(lines) - 1)
    while first <= last and not lines[first].strip():
        first += 1
    while last >= first and not lines[last].strip():
        last -= 1
    if first > last:
        return []
    body = "\n".join(lines[first : last + 1])
    heading = f"{title} / {name}" if name and title else (name or title or "")
    return [
        Section(
            heading=heading,
            start_line=first + 1,
            end_line=last + 1,
            body=body,
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
    ]


class KbIndex:
    """`kb-index.sqlite3` 的读写：不做行级更新，只按文件替换或整体重建。"""

    def __init__(self, path: Path):
        self.path = path

    # ------------------------------------------------------------------ 新鲜度

    def is_current(self, tree: str) -> bool:
        """索引是否完整对应 `tree` 标识的 `kb/` 目录状态；读取失败一律按不可用处理。"""
        try:
            with session(self.path) as conn:
                if self._meta(conn, META_SCHEMA) != str(INDEX_SCHEMA):
                    return False
                if self._meta(conn, META_TREE) != tree:
                    return False
                conn.execute("SELECT COUNT(*) FROM kb_sections").fetchone()
                return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------ 写入

    def rebuild(self, tree: str, documents: Iterable[IndexDocument]) -> None:
        """整体重建：先丢弃现有文件，再按当前资料重写，最后写入 tree 标识。"""
        self._discard()
        with session(self.path) as conn, write(conn):
            self._create(conn)
            for document in documents:
                self._insert(conn, document)
            self._set(conn, META_SCHEMA, str(INDEX_SCHEMA))
            self._set(conn, META_TREE, tree)

    def replace(self, document: IndexDocument, *, tree: str) -> None:
        """只替换这一份资料的条目；调用方保证索引在 `tree` 之前是完整的。"""
        with session(self.path) as conn, write(conn):
            self._create(conn)
            self._drop_path(conn, document.path)
            self._insert(conn, document)
            self._set(conn, META_SCHEMA, str(INDEX_SCHEMA))
            self._set(conn, META_TREE, tree)

    def _discard(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists():
                path.unlink()

    @staticmethod
    def _create(conn: sqlite3.Connection) -> None:
        for statement in CREATE_SECTIONS:
            conn.execute(statement)

    @staticmethod
    def _drop_path(conn: sqlite3.Connection, path: str) -> None:
        conn.execute(
            "DELETE FROM kb_sections_fts WHERE rowid IN "
            "(SELECT row_id FROM kb_sections WHERE path = ?)",
            (path,),
        )
        conn.execute("DELETE FROM kb_sections WHERE path = ?", (path,))

    @staticmethod
    def _insert(conn: sqlite3.Connection, document: IndexDocument) -> None:
        meta = document.meta
        source = meta.get("source") or {}
        title = meta.get("title")
        tags = meta.get("tags") or []
        tags_text = json.dumps(list(tags), ensure_ascii=False)
        for section in document.sections:
            cursor = conn.execute(
                "INSERT INTO kb_sections "
                "(doc_id, path, title, source_kind, tags, heading, start_line, end_line, "
                "commit_sha, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    meta.get("id") or "",
                    document.path,
                    title,
                    source.get("kind") if isinstance(source, dict) else None,
                    tags_text,
                    section.heading,
                    section.start_line,
                    section.end_line,
                    document.commit,
                    section.content_hash,
                ),
            )
            conn.execute(
                "INSERT INTO kb_sections_fts (rowid, title, heading, tags, body) "
                "VALUES (?,?,?,?,?)",
                (cursor.lastrowid, title or "", section.heading, tags_text, section.body),
            )

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM kb_index_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    @staticmethod
    def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO kb_index_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------------ 检索

    def search(
        self,
        *,
        terms: list[str],
        source_kind: str | None,
        tag: str | None,
        max_results: int,
    ) -> list[Hit]:
        """按关键词检索分节，按字段优先级与相关度排序；摘要按词命中处截取。

        排序在 Python 里做（字段优先级先于相关度），必须先对全部候选排完序再截断，
        否则最相关的结果可能被任意丢弃；正文只在截断后为最终命中取回。
        """
        with session(self.path) as conn:
            rows = self._candidates(conn, terms=terms, source_kind=source_kind, tag=tag)
            ranked = [(self._rank_key(row, terms), row) for row in rows]
            ranked.sort(key=lambda item: item[0])
            top = [row for _, row in ranked[:max_results]]
            bodies = self._bodies(conn, [row["row_id"] for row in top]) if top else {}
        return [self._hit(row, terms, bodies[row["row_id"]]) for row in top]

    def _candidates(
        self,
        conn: sqlite3.Connection,
        *,
        terms: list[str],
        source_kind: str | None,
        tag: str | None,
    ) -> list[sqlite3.Row]:
        condition, params, score = _condition(terms)
        clauses = [condition]
        if source_kind is not None:
            clauses.append("kb_sections.source_kind = ?")
            params.append(source_kind)
        if tag is not None:
            clauses.append("kb_sections.tags LIKE ? ESCAPE '\\'")
            params.append(f'%"{_escape_like(tag)}"%')
        sql = (
            "SELECT kb_sections.row_id, kb_sections.doc_id, kb_sections.path, "
            "kb_sections.title, kb_sections.source_kind, kb_sections.tags, "
            "kb_sections.heading, kb_sections.start_line, kb_sections.end_line, "
            f"kb_sections.commit_sha, {score} AS score "
            "FROM kb_sections_fts JOIN kb_sections "
            "ON kb_sections.row_id = kb_sections_fts.rowid "
            f"WHERE {' AND '.join(clauses)}"
        )
        return list(conn.execute(sql, params))

    @staticmethod
    def _bodies(conn: sqlite3.Connection, row_ids: list[int]) -> dict[int, str]:
        placeholders = ",".join("?" * len(row_ids))
        rows = conn.execute(
            f"SELECT rowid, body FROM kb_sections_fts WHERE rowid IN ({placeholders})",
            row_ids,
        )
        return {row["rowid"]: row["body"] for row in rows}

    @staticmethod
    def _rank_key(row: sqlite3.Row, terms: list[str]) -> tuple[int, float, str, int]:
        return (
            field_rank(row["title"], row["heading"], _as_tags(row["tags"]), terms),
            float(row["score"] or 0.0),
            row["path"],
            row["start_line"],
        )

    @staticmethod
    def _hit(row: sqlite3.Row, terms: list[str], body: str) -> Hit:
        tags = _as_tags(row["tags"])
        return Hit(
            doc_id=row["doc_id"],
            path=row["path"],
            title=row["title"],
            source_kind=row["source_kind"],
            tags=tags,
            heading=row["heading"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            commit=row["commit_sha"],
            snippet=snippet(body, terms),
            score=float(row["score"] or 0.0),
            rank=field_rank(row["title"], row["heading"], tags, terms),
        )


def _condition(terms: list[str]) -> tuple[str, list[str], str]:
    """把检索词转成 SQL 条件：全部词够长走 FTS5，含短词时在同一张表上做包含匹配。"""
    if all(len(term) >= FTS_MIN_TERM for term in terms):
        expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        return "kb_sections_fts MATCH ?", [expression], "bm25(kb_sections_fts)"
    clauses: list[str] = []
    params: list[str] = []
    for term in terms:
        pattern = f"%{_escape_like(term.lower())}%"
        clauses.append(
            "(lower(kb_sections_fts.title) LIKE ? ESCAPE '\\' OR "
            "lower(kb_sections_fts.heading) LIKE ? ESCAPE '\\' OR "
            "lower(kb_sections_fts.tags) LIKE ? ESCAPE '\\' OR "
            "lower(kb_sections_fts.body) LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 4)
    return " AND ".join(clauses), params, "0.0"


def _escape_like(value: str) -> str:
    for char in ("\\", "%", "_"):
        value = value.replace(char, f"\\{char}")
    return value


def _as_tags(text: str) -> list[str]:
    try:
        loaded = json.loads(text)
    except ValueError:
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def snippet(text: str, terms: list[str], *, width: int = 160) -> str:
    """命中的一段摘要：优先从第一个真实命中处截取，命中不在正文里时取开头。"""
    flat = " ".join(text.split())
    folded = flat.casefold()
    positions = [position for position in (folded.find(term.casefold()) for term in terms)]
    found = [position for position in positions if position >= 0]
    start = max(0, min(found) - width // 4) if found else 0
    window = flat[start : start + width]
    return f"{'…' if start > 0 else ''}{window}{'…' if start + width < len(flat) else ''}"


def normalize_terms(query: str) -> list[str]:
    return [term for term in (query or "").split() if term]


def field_rank(title: str | None, heading: str, tags: list[str], terms: list[str]) -> int:
    """命中的最优先字段：标题 0、分节标题 1、标签 2、正文 3；任一词命中更高优先级即靠前。"""
    ranks = []
    for term in terms:
        needle = term.casefold()
        if needle in (title or "").casefold():
            ranks.append(0)
        elif needle in heading.casefold():
            ranks.append(1)
        elif any(needle in tag.casefold() for tag in tags):
            ranks.append(2)
        else:
            ranks.append(3)
    return min(ranks) if ranks else 3
