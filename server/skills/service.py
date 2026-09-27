"""技能服务：变更的记录与应用规则、目录、加载记录与版本恢复。

写入统一走 `record_change`：管理页与前台对话的修改直接应用并留档，后台复盘对用户
手写技能只能提出待确认建议；应用前校验内容版本，过期记 `conflict`。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from server.config import get_settings
from server.db import session, write
from server.errors import SkillConflictError, SkillUnknownError, SkillValidationError
from server.skills.models import (
    DESCRIPTION_LIMIT,
    SKILL_ID_PATTERN,
    ChangeAction,
    ChangeActor,
    ChangeStatus,
    Skill,
    SkillOrigin,
    SkillState,
    public_change_reason,
)
from server.skills.repository import (
    SkillRepository,
    now,
    safe_attachment_path,
    skill_dir_path,
)
from server.skills.review_state import reset_after_user_write

logger = logging.getLogger(__name__)

MANUAL_SKILL_LIMIT = 10
MANUAL_BODY_BUDGET = 40000
CATALOG_LIMIT = 50
CREATE_ORIGINS: dict[ChangeActor, SkillOrigin] = {
    ChangeActor.USER: SkillOrigin.USER,
    ChangeActor.FOREGROUND: SkillOrigin.EXPLICIT,
    ChangeActor.REVIEW: SkillOrigin.REVIEW,
}


@dataclass(frozen=True)
class ChangeRequest:
    action: ChangeAction
    payload: dict
    actor: ChangeActor
    reason: str
    skill_id: str | None = None
    base_revision: str | None = None
    review_job_id: str | None = None
    evidence_item_ids: tuple[str, ...] = ()
    change_id: str | None = None


class SkillService:
    def __init__(self, data_dir: Path | None = None, db_path: Path | None = None):
        settings = get_settings()
        self.repository = SkillRepository(data_dir or settings.data_dir)
        self.db_path = db_path or settings.db_path

    # ------------------------------------------------------------------ 读取

    def get(self, skill_id: str) -> Skill:
        skill = self.repository.load(skill_id)
        if skill is None:
            raise SkillUnknownError(skill_id)
        return skill

    def loadable(self, skill_id: str) -> Skill:
        """可进入上下文的技能：存在、启用，且磁盘内容相对版本库没有未提交改动。"""

        skill = self.repository.load_consistent(skill_id)
        if skill is None:
            raise SkillUnknownError(skill_id)
        if skill.state is not SkillState.ACTIVE:
            raise SkillUnknownError(skill_id)
        return skill

    def catalog(self, state: SkillState = SkillState.ACTIVE) -> list[dict]:
        """目录条目：指定状态（默认 active）的技能，按最近加载时间排序。"""

        last_loaded = self._last_loaded()
        entries = []
        for skill in self.repository.load_all():
            loaded_at = last_loaded.get(skill.skill_id)
            entries.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "origin": skill.origin.value,
                    "managed": skill.managed,
                    "state": skill.state.value,
                    "revision": skill.revision,
                    "updated_at": skill.updated_at,
                    "last_loaded_at": loaded_at,
                    "sort_key": loaded_at or "",
                }
            )
        entries.sort(key=lambda item: item["sort_key"], reverse=True)
        for entry in entries:
            entry.pop("sort_key", None)
        return [entry for entry in entries if entry["state"] == state.value]

    def versions(self, skill_id: str) -> list[dict]:
        self.get(skill_id)
        return [
            {
                "commit": version.commit,
                "revision": version.revision,
                "created_at": version.created_at,
                "change_id": version.change_id,
                "actor": version.actor,
                "reason": public_change_reason(version.reason, version.actor),
                "skill": version.skill,
            }
            for version in self.repository.versions(skill_id)
        ]

    def version_detail(self, skill_id: str, revision: str) -> Skill:
        for version in self.repository.versions(skill_id):
            if version.revision == revision:
                return version.skill
        raise SkillUnknownError(f"版本不存在：{skill_id}@{revision[:12]}")

    def changes(self, status: str | None = None, skill_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM skill_changes"
        conditions: list[str] = []
        params: list[str] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if skill_id is not None:
            conditions.append("skill_id = ?")
            params.append(skill_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        with session(self.db_path) as conn:
            return [_change_view(row) for row in conn.execute(query, params)]

    # ------------------------------------------------------------------ 变更

    def record_change(self, request: ChangeRequest) -> dict:
        """登记一次变更；user/foreground 直接应用，review 视目标技能的 managed 决定。"""

        if request.change_id is not None:
            with session(self.db_path) as conn:
                previous = _find_change(conn, request.change_id)
            if previous is not None:
                if previous["status"] != ChangeStatus.PROPOSED.value:
                    return _change_view(previous)
                if request.actor is ChangeActor.REVIEW and self._review_may_apply(request):
                    skill_id = previous["skill_id"]
                    if skill_id and any(
                        version.change_id == request.change_id
                        for version in self.repository.versions(skill_id)
                    ):
                        with session(self.db_path) as conn, write(conn):
                            conn.execute(
                                "UPDATE skill_changes SET status='applied',applied_at=? WHERE id=?",
                                (now(), request.change_id),
                            )
                        return {**_change_view(previous), "status": "applied"}
                    return self._apply(request.change_id, expected_revision=request.base_revision)
                return _change_view(previous)

        errors = self._validate(request)
        if errors:
            raise SkillValidationError(errors)
        change_id = request.change_id or f"chg_{uuid.uuid4().hex[:16]}"
        row = {
            "id": change_id,
            "review_job_id": request.review_job_id,
            "skill_id": request.skill_id or request.payload.get("skill_id"),
            "action": request.action.value,
            "payload": json.dumps(request.payload, ensure_ascii=False),
            "base_revision": request.base_revision,
            "reason": request.reason,
            "evidence_item_ids": json.dumps(list(request.evidence_item_ids)),
            "actor": request.actor.value,
            "status": ChangeStatus.PROPOSED.value,
            "created_at": now(),
            "applied_at": None,
        }
        with session(self.db_path) as conn, write(conn):
            conn.execute(
                "INSERT INTO skill_changes (id, review_job_id, skill_id, action, payload, "
                "base_revision, reason, evidence_item_ids, actor, status, created_at, applied_at) "
                "VALUES (:id, :review_job_id, :skill_id, :action, :payload, :base_revision, "
                ":reason, :evidence_item_ids, :actor, :status, :created_at, :applied_at)",
                row,
            )
        direct = request.actor in (ChangeActor.USER, ChangeActor.FOREGROUND) or (
            request.actor is ChangeActor.REVIEW and self._review_may_apply(request)
        )
        if direct:
            result = self._apply(change_id, expected_revision=request.base_revision)
            if request.actor in (ChangeActor.USER, ChangeActor.FOREGROUND):
                with session(self.db_path) as conn, write(conn):
                    reset_after_user_write(conn)
            return result
        return _change_view(row)

    def approve(self, change_id: str, expected_revision: str) -> dict:
        with session(self.db_path) as conn:
            row = _find_change(conn, change_id)
        if row is None:
            raise SkillUnknownError(change_id)
        if row["status"] != ChangeStatus.PROPOSED.value:
            current = self._current_revision(row["skill_id"])
            raise SkillConflictError(current or (row["base_revision"] or ""))
        result = self._apply(change_id, expected_revision=expected_revision)
        with session(self.db_path) as conn, write(conn):
            reset_after_user_write(conn)
        return result

    def reject(self, change_id: str) -> dict:
        with session(self.db_path) as conn, write(conn):
            row = _find_change(conn, change_id)
            if row is None:
                raise SkillUnknownError(change_id)
            if row["status"] != ChangeStatus.PROPOSED.value:
                raise SkillConflictError(row["base_revision"] or "")
            conn.execute(
                "UPDATE skill_changes SET status = ? WHERE id = ?",
                (ChangeStatus.REJECTED.value, change_id),
            )
            row = dict(row, status=ChangeStatus.REJECTED.value)
        return _change_view(row)

    def _apply(self, change_id: str, *, expected_revision: str | None) -> dict:
        with session(self.db_path) as conn:
            row = _find_change(conn, change_id)
        assert row is not None
        request = _row_to_request(row)
        try:
            skill = self._apply_writes(request, expected_revision, change_id)
        except SkillConflictError as error:
            self._update_status(change_id, ChangeStatus.CONFLICT)
            raise error
        except (SkillValidationError, SkillUnknownError):
            self._update_status(change_id, ChangeStatus.REJECTED)
            raise
        except ValueError as error:
            # 兜底：载荷里的非法标识等不该变成 500，也不该留下 proposed 记录。
            self._update_status(change_id, ChangeStatus.REJECTED)
            raise SkillValidationError([{"field": "payload", "message": str(error)}]) from error
        with session(self.db_path) as conn, write(conn):
            conn.execute(
                "UPDATE skill_changes SET status = ?, applied_at = ? WHERE id = ?",
                (ChangeStatus.APPLIED.value, now(), change_id),
            )
        if skill is not None:
            return {
                **_change_view(row),
                "status": ChangeStatus.APPLIED.value,
                "applied_at": now(),
                "skill": skill_view(skill),
            }
        return {**_change_view(row), "status": ChangeStatus.APPLIED.value, "applied_at": now()}

    def _apply_writes(
        self, request: ChangeRequest, expected_revision: str | None, change_id: str
    ) -> Skill | None:
        """按动作计算并落盘写入；返回写后的技能（create 时即新技能）。"""

        action = request.action
        payload = request.payload
        if action is ChangeAction.CREATE:
            skill_id = payload["skill_id"]
            if self.repository.load(skill_id) is not None:
                raise SkillValidationError(
                    [{"field": "skill_id", "message": f"技能标识已存在：{skill_id}"}]
                )
            origin = CREATE_ORIGINS[request.actor]
            skill = Skill(
                skill_id=skill_id,
                name=payload["name"],
                description=payload["description"],
                origin=origin,
                # 手写技能默认受保护；用户可在管理页明确开启后台直接修改。
                managed=(
                    False
                    if origin is SkillOrigin.USER
                    else True
                    if origin is SkillOrigin.REVIEW
                    else bool(payload.get("managed", True))
                ),
                state=SkillState.ACTIVE,
                created_at=now(),
                updated_at=now(),
                body=payload["body"],
            )
            attachments = {
                path: content.encode("utf-8")
                for path, content in (payload.get("attachments") or {}).items()
            }
            self.repository.commit_skill(skill, _commit_message(request, change_id), attachments)
            return self.get(skill_id)

        skill_id = request.skill_id or payload.get("skill_id")
        current = self.get(skill_id)
        if expected_revision is None:
            raise SkillValidationError(
                [{"field": "expected_revision", "message": "修改既有技能必须传当前内容版本"}]
            )
        if expected_revision != current.revision:
            raise SkillConflictError(current.revision)
        if action is ChangeAction.PATCH:
            body = payload.get("body")
            if body is None:
                old, new = payload["old_string"], payload["new_string"]
                occurrences = current.body.count(old)
                if occurrences != 1:
                    raise SkillValidationError(
                        [
                            {
                                "field": "old_string",
                                "message": f"期望恰好出现一次，实际 {occurrences} 次",
                            }
                        ]
                    )
                body = current.body.replace(old, new, 1)
            updated = Skill(
                skill_id=skill_id,
                name=payload.get("name", current.name),
                description=payload.get("description", current.description),
                origin=current.origin,
                managed=current.managed,
                state=current.state,
                created_at=current.created_at,
                updated_at=now(),
                body=body,
                files=current.files,
            )
            errors = _frontmatter_errors(updated)
            if errors:
                raise SkillValidationError(errors)
            self.repository.commit_skill(updated, _commit_message(request, change_id))
            return updated

        if action is ChangeAction.WRITE_FILE:
            self.repository.commit_writes(
                [
                    (
                        f"{skill_dir_path(skill_id)}/{payload['relative_path']}",
                        payload["content"].encode("utf-8"),
                    )
                ],
                _commit_message(request, change_id),
            )
            return self.get(skill_id)
        if action is ChangeAction.REMOVE_FILE:
            relative = payload["relative_path"]
            if relative not in {item.relative_path for item in current.files}:
                raise SkillUnknownError(f"附件不存在：{relative}")
            self.repository.commit_writes(
                [(f"{skill_dir_path(skill_id)}/{relative}", None)],
                _commit_message(request, change_id),
            )
            return self.get(skill_id)
        raise SkillValidationError([{"field": "action", "message": f"未知动作：{action}"}])

    # ------------------------------------------------------------------ 状态与管理策略

    def archive(self, skill_id: str) -> Skill:
        return self._set_state(skill_id, SkillState.ARCHIVED)

    def restore(self, skill_id: str) -> Skill:
        return self._set_state(skill_id, SkillState.ACTIVE)

    def set_managed(self, skill_id: str, value: bool) -> Skill:
        skill = self.get(skill_id)
        updated = Skill(**{**_asdict(skill), "managed": value, "updated_at": now()})
        self.repository.commit_skill(updated, f"[Skills] managed {skill_id} -> {value}")
        with session(self.db_path) as conn, write(conn):
            reset_after_user_write(conn)
        return self.get(skill_id)

    def restore_version(self, skill_id: str, revision: str) -> dict:
        """恢复历史版本：以目标内容整份替换正文，产生新提交不重置历史。"""

        target = self.version_detail(skill_id, revision)
        return self.record_change(
            ChangeRequest(
                action=ChangeAction.PATCH,
                payload={"body": target.body},
                actor=ChangeActor.USER,
                reason=f"恢复历史版本 {revision[:12]}",
                skill_id=skill_id,
                base_revision=self.get(skill_id).revision,
            )
        )

    # ------------------------------------------------------------------ 装配与加载记录

    def validate_selection(self, skill_ids: list[str]) -> list[Skill]:
        """手动选择装配前的校验与解析；返回按提交顺序去重后的技能。"""

        errors: list[dict[str, str]] = []
        unique: list[str] = []
        for skill_id in skill_ids:
            if skill_id not in unique:
                unique.append(skill_id)
        if len(unique) > MANUAL_SKILL_LIMIT:
            errors.append(
                {"field": "skills", "message": f"手动选择最多 {MANUAL_SKILL_LIMIT} 个技能"}
            )
        loaded: list[Skill] = []
        for skill_id in unique:
            loaded.append(self.loadable(skill_id))
        total = sum(len(skill.body) for skill in loaded)
        if total > MANUAL_BODY_BUDGET:
            errors.append(
                {
                    "field": "skills",
                    "message": f"所选正文共 {total} 字符，超过 {MANUAL_BODY_BUDGET} 上限",
                }
            )
        if errors:
            raise SkillValidationError(errors)
        return loaded

    def record_load(self, task_id: str, run_id: str, skill: Skill, source: str) -> None:
        with session(self.db_path) as conn, write(conn):
            conn.execute(
                "INSERT OR REPLACE INTO skill_loads "
                "(run_id, task_id, skill_id, revision, source, loaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, task_id, skill.skill_id, skill.revision, source, now()),
            )

    def task_skill_usage(self, task_id: str) -> list[dict]:
        with session(self.db_path) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT run_id, skill_id, revision, source, loaded_at FROM skill_loads "
                    "WHERE task_id = ? ORDER BY loaded_at",
                    (task_id,),
                )
            ]

    def skill_usage(self, skill_id: str, limit: int = 20) -> list[dict]:
        """一个技能最近的加载记录，供详情页展示使用情况。"""

        with session(self.db_path) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT run_id, task_id, revision, source, loaded_at FROM skill_loads "
                    "WHERE skill_id = ? ORDER BY loaded_at DESC LIMIT ?",
                    (skill_id, limit),
                )
            ]

    # ------------------------------------------------------------------ 内部

    def _set_state(self, skill_id: str, state: SkillState) -> Skill:
        skill = self.get(skill_id)
        updated = Skill(**{**_asdict(skill), "state": state, "updated_at": now()})
        self.repository.commit_skill(updated, f"[Skills] state {skill_id} -> {state.value}")
        with session(self.db_path) as conn, write(conn):
            reset_after_user_write(conn)
        return updated

    def _current_revision(self, skill_id: str | None) -> str | None:
        if not skill_id:
            return None
        skill = self.repository.load(skill_id)
        return skill.revision if skill is not None else None

    def _review_may_apply(self, request: ChangeRequest) -> bool:
        if request.action is ChangeAction.CREATE:
            return True
        skill = self.repository.load(request.skill_id or request.payload.get("skill_id", ""))
        return skill is not None and skill.managed

    def _validate(self, request: ChangeRequest) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        payload = request.payload
        action = request.action
        if not request.reason.strip():
            errors.append({"field": "reason", "message": "必须说明修改原因"})
        if request.actor is ChangeActor.REVIEW and request.review_job_id:
            with session(self.db_path) as conn:
                review = conn.execute(
                    "SELECT from_seq,through_seq,generation,status FROM skill_reviews WHERE id=?",
                    (request.review_job_id,),
                ).fetchone()
                state = conn.execute(
                    "SELECT generation FROM skill_review_state WHERE id=1"
                ).fetchone()
                if (
                    review is None
                    or review["status"] != "applying"
                    or review["generation"] != state[0]
                ):
                    errors.append({"field": "review_job_id", "message": "复盘已过期或不可写"})
                if not request.evidence_item_ids:
                    errors.append(
                        {"field": "evidence_item_ids", "message": "复盘变更必须有轨迹依据"}
                    )
        for item_id in request.evidence_item_ids:
            with session(self.db_path) as conn:
                if (
                    request.actor is ChangeActor.REVIEW
                    and request.review_job_id
                    and review is not None
                ):
                    found = conn.execute(
                        "SELECT 1 FROM task_timeline_items i JOIN skill_review_turns t "
                        "ON t.run_id=i.run_id WHERE i.item_id=? AND t.seq>? AND t.seq<=?",
                        (item_id, review["from_seq"], review["through_seq"]),
                    ).fetchone()
                else:
                    found = conn.execute(
                        "SELECT 1 FROM task_timeline_items WHERE item_id = ?", (item_id,)
                    ).fetchone()
            if found is None:
                errors.append(
                    {"field": "evidence_item_ids", "message": f"依据条目不存在：{item_id}"}
                )
        if action is ChangeAction.CREATE:
            skill_id = payload.get("skill_id", "")
            if not SKILL_ID_PATTERN.match(skill_id):
                errors.append({"field": "skill_id", "message": f"技能标识不合法：{skill_id}"})
            errors.extend(
                _frontmatter_errors(
                    Skill(
                        skill_id=str(skill_id),
                        name=str(payload.get("name", "")),
                        description=str(payload.get("description", "")),
                        origin=CREATE_ORIGINS[request.actor],
                        managed=False,
                        state=SkillState.ACTIVE,
                        created_at="",
                        updated_at="",
                        body=str(payload.get("body", "")),
                    )
                )
            )
            for relative in payload.get("attachments") or {}:
                try:
                    safe_attachment_path(relative)
                except ValueError as error:
                    errors.append({"field": "attachments", "message": str(error)})
        elif action is ChangeAction.PATCH:
            if "body" not in payload and not ("old_string" in payload and "new_string" in payload):
                errors.append(
                    {"field": "payload", "message": "patch 需要整份正文或 old_string/new_string"}
                )
            target = request.skill_id or payload.get("skill_id")
            if target is None:
                errors.append({"field": "skill_id", "message": "缺少目标技能"})
            elif not SKILL_ID_PATTERN.match(str(target)):
                errors.append({"field": "skill_id", "message": f"技能标识不合法：{target}"})
        elif action in (ChangeAction.WRITE_FILE, ChangeAction.REMOVE_FILE):
            target = request.skill_id or payload.get("skill_id")
            if target is not None and not SKILL_ID_PATTERN.match(str(target)):
                errors.append({"field": "skill_id", "message": f"技能标识不合法：{target}"})
            relative = payload.get("relative_path", "")
            try:
                safe_attachment_path(relative)
            except ValueError as error:
                errors.append({"field": "relative_path", "message": str(error)})
            if action is ChangeAction.WRITE_FILE and "content" not in payload:
                errors.append({"field": "content", "message": "缺少附件内容"})
        return errors

    def _update_status(self, change_id: str, status: ChangeStatus) -> None:
        with session(self.db_path) as conn, write(conn):
            conn.execute(
                "UPDATE skill_changes SET status = ? WHERE id = ?", (status.value, change_id)
            )

    def _last_loaded(self) -> dict[str, str]:
        with session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT skill_id, MAX(loaded_at) AS latest FROM skill_loads GROUP BY skill_id"
            )
            return {row["skill_id"]: row["latest"] for row in rows}


def _asdict(skill: Skill) -> dict:
    # dataclasses.asdict 会深拷贝 files 元组，这里字段皆不可变，浅展开即可。
    return {
        "skill_id": skill.skill_id,
        "name": skill.name,
        "description": skill.description,
        "origin": skill.origin,
        "managed": skill.managed,
        "state": skill.state,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
        "body": skill.body,
        "files": skill.files,
    }


def skill_view(skill: Skill) -> dict:
    """技能的 JSON 视图；revision 是 property，需显式带出。"""

    return {
        "skill_id": skill.skill_id,
        "name": skill.name,
        "description": skill.description,
        "origin": skill.origin.value,
        "managed": skill.managed,
        "state": skill.state.value,
        "body": skill.body,
        "revision": skill.revision,
        "files": [{"path": item.relative_path, "hash": item.content_hash} for item in skill.files],
    }


def _frontmatter_errors(skill: Skill) -> list[dict[str, str]]:
    """frontmatter 字段规则（契约 §2）：显示名与描述必填，描述不超上限，正文非空。"""

    errors: list[dict[str, str]] = []
    if not skill.name.strip():
        errors.append({"field": "name", "message": "不能为空"})
    if not skill.description.strip():
        errors.append({"field": "description", "message": "不能为空"})
    elif len(skill.description) > DESCRIPTION_LIMIT:
        errors.append({"field": "description", "message": f"超过 {DESCRIPTION_LIMIT} 字符上限"})
    if not skill.body.strip():
        errors.append({"field": "body", "message": "正文不能为空"})
    return errors


def _commit_message(request: ChangeRequest, change_id: str) -> str:
    """提交说明的元数据走尾注，`versions()` 读回 change_id 与 actor。"""

    target = request.skill_id or request.payload.get("skill_id", "")
    reason = " ".join(request.reason.split())
    return "\n".join(
        [
            f"[Skills] {request.action.value} {target}".rstrip(),
            "",
            f"change-id: {change_id}",
            f"actor: {request.actor.value}",
            f"reason: {reason}",
        ]
    )


def _find_change(conn, change_id: str):
    return conn.execute("SELECT * FROM skill_changes WHERE id = ?", (change_id,)).fetchone()


def _row_to_request(row) -> ChangeRequest:
    return ChangeRequest(
        action=ChangeAction(row["action"]),
        payload=json.loads(row["payload"]),
        actor=ChangeActor(row["actor"]),
        reason=row["reason"],
        skill_id=row["skill_id"],
        base_revision=row["base_revision"],
        review_job_id=row["review_job_id"],
        evidence_item_ids=tuple(json.loads(row["evidence_item_ids"])),
    )


def _change_view(row) -> dict:
    view = dict(row)
    view["reason"] = public_change_reason(row["reason"], row["actor"])
    view["payload"] = json.loads(row["payload"])
    view["evidence_item_ids"] = json.loads(row["evidence_item_ids"])
    return view
