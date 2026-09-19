"""Skill 目录：可用列表、轮次过滤、自动匹配、使用记录。"""

from __future__ import annotations

from server.db import session, write
from server.skills import repository
from server.skills.models import SkillMeta


def available_catalog() -> list[SkillMeta]:
    """返回 status=approved 且哈希一致的可用 Skill。"""
    from server.skills.models import SkillStatus
    from server.skills.validation import compute_skill_hash

    return [
        s
        for s in repository.list_all()
        if s.status == SkillStatus.APPROVED
        and s.approved_version == s.content_hash == compute_skill_hash(s)
    ]


def record_skill_usage(run_id: str, skill_id: str, revision: str, source: str, path=None) -> None:
    """记录 Skill 使用到 SQLite。"""
    from server.sessions.service import timestamp

    now = timestamp()
    with session(path) as conn, write(conn):
        conn.execute(
            "INSERT OR IGNORE INTO skill_run_links (run_id, skill_id, revision, source, loaded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, skill_id, revision, source, now),
        )
