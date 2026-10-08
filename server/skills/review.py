"""跨任务 Skill 复盘：持久化计数、证据窗口和候选变更。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from server.config import get_settings
from server.db import session, write
from server.errors import SkillConflictError, SkillUnknownError, SkillValidationError
from server.sessions.service import timestamp
from server.skills.models import REVIEW_REASON_REF, ChangeAction, ChangeActor
from server.skills.service import ChangeRequest, SkillService

logger = logging.getLogger(__name__)
REVIEW_INSTRUCTIONS = (
    "你是 Pebble 的后台技能复盘程序，不面向用户回复。"
    "只依据给出的真实执行轨迹总结以后同类任务的做法。"
    "找用户对步骤、方法、格式的纠正，经过尝试验证的有效方法，或现有技能的错误和缺步。"
    "优先修改本窗口实际加载且相关的技能，其次修改其他相关技能，再考虑补充参考资料；"
    "仅当没有相关技能时创建覆盖一类任务的新技能。修改前用 skill_view 重新读取当前正文及版本。"
    "不要保存暂时性故障、未解决的失败、一次性经过、消息原文或具体参数。"
    "每条修改调用 skill_manage，说明原因并提供轨迹条目前的短编号；"
    "reason 会直接展示给用户：用自然语言说明纠正、验证结果和可复用做法，"
    "不要在 reason 中写 E1 等轨迹短编号、任务 ID 或其他内部标识；"
    "短编号只放在 evidence_refs_to_use。"
    "没有可靠经验时不调用工具，直接回答‘无’。"
    "只能使用本会话提供的技能工具，不能执行外部操作。"
)
MAX_REVIEW_CHARS = 100_000


def validate_review_reason(reason: str, evidence_refs: dict[str, str]) -> None:
    """复盘理由直接面向用户；内部轨迹标识只能进入结构化依据字段。"""
    if not reason.strip():
        raise ValueError("复盘变更必须说明原因")
    if REVIEW_REASON_REF.search(reason) or any(
        item_id in reason for item_id in evidence_refs.values()
    ):
        raise ValueError(
            "reason 会直接展示给用户，请用自然语言说明依据和做法；"
            "轨迹短编号只填在 evidence_refs_to_use，不能写进 reason"
        )


class SkillReviewGateway(Protocol):
    async def review_skills(
        self,
        review_id: str,
        anchor_task_id: str,
        instructions: str,
        material: str,
        model: str,
        evidence_refs: dict[str, str],
    ) -> list[dict]: ...


def resolve_evidence_refs(refs: list[str], available: dict[str, str]) -> list[str]:
    """只把本次复盘窗口的短编号转换为真实轨迹 ID。"""
    if not refs:
        raise ValueError("复盘变更必须提供轨迹依据编号")
    unknown = [ref for ref in refs if ref not in available]
    if unknown:
        raise ValueError(f"无效轨迹依据编号：{', '.join(unknown)}；请使用轨迹条目前的编号")
    return list(dict.fromkeys(available[ref] for ref in refs))


def record_completed_turn(conn, run_id: str, task_id: str) -> None:
    """和轮次 done 状态在同一事务内写入，完成顺序由 SQLite 序号决定。"""
    conn.execute(
        "INSERT OR IGNORE INTO skill_review_turns (run_id, task_id, created_at) VALUES (?,?,?)",
        (run_id, task_id, timestamp()),
    )


class SkillReviewScheduler:
    def __init__(
        self,
        skills: SkillService,
        *,
        path: Path | None = None,
        interval: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.skills = skills
        self.path = path or settings.db_path
        self.interval = interval if interval is not None else settings.skill_review_interval
        self.enabled = enabled if enabled is not None else settings.skill_review_enabled

    def enqueue_if_due(self) -> dict | None:
        if not self.enabled:
            return None
        with session(self.path) as conn, write(conn):
            open_row = conn.execute(
                "SELECT * FROM skill_reviews "
                "WHERE status IN ('pending','running','applying') LIMIT 1"
            ).fetchone()
            if open_row:
                return None
            state = conn.execute("SELECT * FROM skill_review_state WHERE id=1").fetchone()
            maximum = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM skill_review_turns"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT f.* FROM skill_reviews f WHERE f.status='failed' AND f.generation=? "
                "AND NOT EXISTS (SELECT 1 FROM skill_reviews c WHERE c.status='completed' "
                "AND c.generation=f.generation AND c.from_seq=f.from_seq "
                "AND c.through_seq=f.through_seq) "
                "ORDER BY created_at DESC LIMIT 1",
                (state["generation"],),
            ).fetchone()
            remainder = conn.execute(
                "SELECT * FROM skill_reviews WHERE status='completed' AND generation=? "
                "AND target_seq>through_seq ORDER BY created_at DESC LIMIT 1",
                (state["generation"],),
            ).fetchone()
            if failed:
                if maximum <= failed["retry_after_seq"]:
                    return None
                has_candidates = conn.execute(
                    "SELECT 1 FROM skill_review_candidates WHERE review_id=? LIMIT 1",
                    (failed["id"],),
                ).fetchone()
                if has_candidates:
                    conn.execute(
                        "UPDATE skill_reviews SET status='applying',error=NULL WHERE id=?",
                        (failed["id"],),
                    )
                    return self.get(failed["id"], conn=conn)
                from_seq, through_seq = failed["from_seq"], failed["through_seq"]
                anchor = failed["anchor_task_id"]
                target_seq = failed["target_seq"]
            elif remainder and state["cursor_seq"] == remainder["through_seq"]:
                from_seq, through_seq = remainder["through_seq"], remainder["target_seq"]
                target_seq = through_seq
                anchor = remainder["anchor_task_id"]
            else:
                turns = conn.execute(
                    "SELECT seq, task_id FROM skill_review_turns WHERE seq>? ORDER BY seq LIMIT ?",
                    (state["cursor_seq"], self.interval),
                ).fetchall()
                if len(turns) < self.interval:
                    return None
                from_seq, through_seq = state["cursor_seq"], turns[-1]["seq"]
                target_seq = through_seq
                anchor = turns[-1]["task_id"]
            review_id = str(uuid4())
            conn.execute(
                "INSERT INTO skill_reviews "
                "(id,from_seq,through_seq,target_seq,generation,anchor_task_id,"
                "status,created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                (
                    review_id,
                    from_seq,
                    through_seq,
                    target_seq,
                    state["generation"],
                    anchor,
                    timestamp(),
                ),
            )
            return self.get(review_id, conn=conn)

    def enqueue_manual(self) -> dict:
        """手动登记一次复盘：覆盖自上次完成以来的全部轮次，不受间隔与自动开关限制。

        已有在途复盘时幂等返回那一条，与 enqueue_if_due 共用"同一时刻最多一个在途"索引。
        没有未复盘轮次时仍登记，执行侧会以"没有可审阅的轨迹"收尾，不调用模型。
        """
        with session(self.path) as conn, write(conn):
            open_row = conn.execute(
                "SELECT id FROM skill_reviews "
                "WHERE status IN ('pending','running','applying') LIMIT 1"
            ).fetchone()
            if open_row:
                return self.get(open_row["id"], conn=conn)
            state = conn.execute("SELECT * FROM skill_review_state WHERE id=1").fetchone()
            latest = conn.execute(
                "SELECT seq, task_id FROM skill_review_turns ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            through = latest["seq"] if latest else state["cursor_seq"]
            review_id = str(uuid4())
            conn.execute(
                "INSERT INTO skill_reviews "
                "(id,from_seq,through_seq,target_seq,generation,anchor_task_id,"
                "status,origin,created_at) VALUES (?,?,?,?,?,?,'pending','manual',?)",
                (
                    review_id,
                    state["cursor_seq"],
                    through,
                    through,
                    state["generation"],
                    latest["task_id"] if latest else "",
                    timestamp(),
                ),
            )
            return self.get(review_id, conn=conn)

    def get(self, review_id: str, *, conn=None) -> dict:
        if conn is None:
            with session(self.path) as opened:
                return self.get(review_id, conn=opened)
        row = conn.execute("SELECT * FROM skill_reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise KeyError(review_id)
        result = dict(row)
        result["failure"] = json.loads(result["failure"]) if result["failure"] else None
        return result

    def pending(self) -> list[dict]:
        with session(self.path) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM skill_reviews WHERE status IN ('pending','applying') "
                    "ORDER BY created_at"
                )
            ]

    def recover(self) -> None:
        with session(self.path) as conn, write(conn):
            # 模型尚未完整返回时没有候选可应用；下一个用户完成轮再试原窗口。
            maximum = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM skill_review_turns"
            ).fetchone()[0]
            conn.execute(
                "UPDATE skill_reviews SET status='failed', retry_after_seq=?, "
                "error='服务重启时复盘尚未完成', finished_at=? WHERE status='running'",
                (maximum, timestamp()),
            )

    def claim(self, review_id: str) -> dict | None:
        with session(self.path) as conn, write(conn):
            changed = conn.execute(
                "UPDATE skill_reviews SET status='running' WHERE id=? AND status='pending'",
                (review_id,),
            )
            return self.get(review_id, conn=conn) if changed.rowcount else None

    def fail(self, review_id: str, error: str, *, failure: dict | None = None) -> None:
        with session(self.path) as conn, write(conn):
            maximum = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM skill_review_turns"
            ).fetchone()[0]
            conn.execute(
                "UPDATE skill_reviews SET status='failed', error=?, retry_after_seq=?, "
                "finished_at=?, failure=? WHERE id=? AND status IN ('running','applying')",
                (
                    error,
                    maximum,
                    timestamp(),
                    json.dumps(failure, ensure_ascii=False) if failure else None,
                    review_id,
                ),
            )

    def _material(self, row: dict) -> tuple[str, int, dict[str, str]]:
        with session(self.path) as conn:
            turns = conn.execute(
                "SELECT t.seq,t.run_id,t.task_id,r.status,r.started_at,r.finished_at,"
                "r.input FROM skill_review_turns t JOIN agent_runs r ON r.run_id=t.run_id "
                "WHERE t.seq>? AND t.seq<=? ORDER BY t.seq",
                (row["from_seq"], row["through_seq"]),
            ).fetchall()
            blocks: list[str] = []
            evidence_refs: dict[str, str] = {}
            through = row["from_seq"]
            for turn in turns:
                items = conn.execute(
                    "SELECT * FROM task_timeline_items WHERE run_id=? ORDER BY sequence",
                    (turn["run_id"],),
                ).fetchall()
                lines = [f"轮次 {turn['seq']} task={turn['task_id']} status={turn['status']}"]
                block_refs: dict[str, str] = {}
                for item in items:
                    ref = f"E{len(evidence_refs) + len(block_refs) + 1}"
                    if item["kind"] == "tool":
                        lines.append(
                            f"[{ref}] tool={item['tool_name']} "
                            f"status={item['tool_status']} "
                            f"arguments={item['tool_arguments']} result={item['tool_result']}"
                        )
                        block_refs[ref] = item["item_id"]
                    elif item["kind"] in ("text", "notice"):
                        lines.append(f"[{ref}] {item['role'] or item['kind']}: {item['text']}")
                        block_refs[ref] = item["item_id"]
                block = "\n".join(lines)
                if blocks and len("\n".join(blocks)) + len(block) > MAX_REVIEW_CHARS:
                    break
                blocks.append(block)
                evidence_refs.update(block_refs)
                through = turn["seq"]
            loads = conn.execute(
                "SELECT l.skill_id,l.revision,l.source FROM skill_loads l "
                "JOIN skill_review_turns t ON t.run_id=l.run_id "
                "WHERE t.seq>? AND t.seq<=? ORDER BY t.seq",
                (row["from_seq"], through),
            ).fetchall()
        catalog = [
            {"skill_id": item["skill_id"], "name": item["name"], "description": item["description"]}
            for item in self.skills.catalog()
        ]
        material = (
            "当前技能目录：" + json.dumps(catalog, ensure_ascii=False) + "\n"
            "本窗口加载过的技能："
            + json.dumps([dict(item) for item in loads], ensure_ascii=False)
            + "\n执行轨迹：\n"
            + "\n\n".join(blocks)
        )
        return material, through, evidence_refs

    async def run(self, review_id: str, gateway: SkillReviewGateway) -> None:
        row = self.get(review_id)
        if row["status"] == "applying":
            self._apply_candidates(review_id)
            return
        material, through, evidence_refs = self._material(row)
        if through == row["from_seq"]:
            self._finish(review_id, through, "没有可审阅的轨迹")
            return
        with session(self.path) as conn:
            task = conn.execute(
                "SELECT model FROM tasks WHERE task_id=?", (row["anchor_task_id"],)
            ).fetchone()
        model = task["model"] if task else "auto"
        candidates = await gateway.review_skills(
            review_id,
            row["anchor_task_id"],
            REVIEW_INSTRUCTIONS,
            material,
            model,
            evidence_refs,
        )
        for candidate in candidates:
            validate_review_reason(candidate["reason"], evidence_refs)
        with session(self.path) as conn, write(conn):
            for ordinal, candidate in enumerate(candidates):
                conn.execute(
                    "INSERT INTO skill_review_candidates "
                    "(review_id,ordinal,action,payload,skill_id,base_revision,reason,"
                    "evidence_item_ids,status) VALUES (?,?,?,?,?,?,?,?,'pending')",
                    (
                        review_id,
                        ordinal,
                        candidate["action"],
                        json.dumps(candidate["payload"], ensure_ascii=False),
                        candidate["payload"].get("skill_id"),
                        candidate.get("expected_revision"),
                        candidate["reason"],
                        json.dumps(candidate["evidence_item_ids"]),
                    ),
                )
            conn.execute(
                "UPDATE skill_reviews SET status='applying', through_seq=? "
                "WHERE id=? AND status='running'",
                (through, review_id),
            )
        self._apply_candidates(review_id)

    def _apply_candidates(self, review_id: str) -> None:
        row = self.get(review_id)
        with session(self.path) as conn:
            candidates = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM skill_review_candidates WHERE review_id=? "
                    "AND status='pending' ORDER BY ordinal",
                    (review_id,),
                )
            ]
        for item in candidates:
            try:
                with session(self.path) as conn:
                    generation = conn.execute(
                        "SELECT generation FROM skill_review_state WHERE id=1"
                    ).fetchone()[0]
                if row["generation"] != generation:
                    raise SkillConflictError("复盘期间 Skill 已由用户或前台修改")
                candidate_key = f"{review_id}:{item['ordinal']}"
                result = self.skills.record_change(
                    ChangeRequest(
                        action=ChangeAction(item["action"]),
                        payload=json.loads(item["payload"]),
                        actor=ChangeActor.REVIEW,
                        reason=item["reason"],
                        skill_id=item["skill_id"],
                        base_revision=item["base_revision"],
                        review_job_id=review_id,
                        evidence_item_ids=tuple(json.loads(item["evidence_item_ids"])),
                        change_id=f"chg_{uuid5(NAMESPACE_URL, candidate_key).hex[:16]}",
                    )
                )
                status = result["status"]
                error = None
                change_id = result["id"]
            except SkillConflictError as exc:
                status, error, change_id = "conflict", str(exc), None
            except (SkillValidationError, SkillUnknownError, ValueError, KeyError) as exc:
                with session(self.path) as conn:
                    generation = conn.execute(
                        "SELECT generation FROM skill_review_state WHERE id=1"
                    ).fetchone()[0]
                status = "conflict" if row["generation"] != generation else "failed"
                error, change_id = str(exc), None
            with session(self.path) as conn, write(conn):
                conn.execute(
                    "UPDATE skill_review_candidates SET status=?,error=?,change_id=? "
                    "WHERE review_id=? AND ordinal=? AND status='pending'",
                    (status, error, change_id, review_id, item["ordinal"]),
                )
        with session(self.path) as conn:
            statuses = [
                item[0]
                for item in conn.execute(
                    "SELECT status FROM skill_review_candidates WHERE review_id=?", (review_id,)
                )
            ]
        failed = sum(status in ("failed", "conflict") for status in statuses)
        summary = (
            f"复盘完成，{failed} 条候选未应用"
            if failed
            else "复盘完成"
            if statuses
            else "无值得保存的经验"
        )
        self._finish(review_id, row["through_seq"], summary)

    def _finish(self, review_id: str, through: int, summary: str) -> None:
        with session(self.path) as conn, write(conn):
            row = self.get(review_id, conn=conn)
            state = conn.execute("SELECT * FROM skill_review_state WHERE id=1").fetchone()
            if row["generation"] == state["generation"]:
                conn.execute(
                    "UPDATE skill_review_state SET cursor_seq=MAX(cursor_seq,?) WHERE id=1",
                    (through,),
                )
            conn.execute(
                "UPDATE skill_reviews SET status='completed',result_summary=?,finished_at=? "
                "WHERE id=?",
                (summary, timestamp(), review_id),
            )
            conn.execute(
                "UPDATE skill_reviews SET status='completed',result_summary='后续复盘已处理' "
                "WHERE status='failed' AND generation=? AND from_seq=? AND target_seq=?",
                (row["generation"], row["from_seq"], row["target_seq"]),
            )
