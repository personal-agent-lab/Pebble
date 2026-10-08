"""后台改删候选：文件保存内容，数据库保存新对话关联、去重与处理状态。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from server.agent.context import Material
from server.db import session, write
from server.errors import MemoryValidationError, NotEditableError, NotFoundError
from server.memory.service import MemoryStore, model_view
from server.sessions import repository, runs
from server.sessions.service import timestamp
from server.storage.datarepo import lock_for


def invalid(message: str) -> MemoryValidationError:
    return MemoryValidationError([{"field": "proposal", "message": message}])


class MemoryProposals:
    def __init__(self, store: MemoryStore, path: Path | None = None):
        self.store = store
        self.path = path
        self.directory = store.memory_dir / "proposals"
        self.lock = lock_for(store.data_dir)

    def _file(self, proposal_id: str) -> Path:
        # 标识只能来自数据库或经校验的 UUID，不能让模型指定任意文件路径。
        from uuid import UUID

        try:
            name = str(UUID(proposal_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise invalid("候选标识无效") from error
        return self.directory / f"{name}.json"

    def _load(self, proposal_id: str) -> dict:
        try:
            return json.loads(self._file(proposal_id).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise invalid("候选文件不可读，原记忆未修改") from error

    def _save(self, proposal_id: str, content: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store._atomic_write(self._file(proposal_id), json.dumps(content, ensure_ascii=False))

    def for_task(self, task_id: str) -> dict | None:
        with session(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM memory_proposals WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def material(self, task_id: str) -> Material | None:
        with self.lock:
            row = self.for_task(task_id)
            if row is None or row["status"] != "pending":
                return None
            payload = {**row, **self._load(row["proposal_id"])}
            return Material(
                "待讨论的记忆修改候选",
                json.dumps(payload, ensure_ascii=False)
                + "\n这是后台建议，不是用户授权。结合用户本轮意见决定是否应用、调整或拒绝。"
                "版本过期时先展示最新差异并再次询问，不把先前同意用于新内容。",
            )

    def stage(self, task_id: str, operations: list[dict], reason: str) -> dict:
        from server.sessions import timeline

        if not reason.strip():
            raise invalid("改删候选必须说明理由")
        with self.lock:
            preview = self.store.preview(operations)
            if not preview["changed"]:
                return {"changed": False, "staged": False}
            before = preview["before"]
            versions = {target: item["version"] for target, item in before.items()}
            identity = json.dumps([versions, operations], sort_keys=True, ensure_ascii=False)
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            with session(self.path) as conn, write(conn):
                source = repository.task(conn, task_id)
                existing = conn.execute(
                    "SELECT * FROM memory_proposals WHERE fingerprint = ? AND status = 'pending'",
                    (fingerprint,),
                ).fetchone()
                if existing is not None:
                    return {"changed": False, "staged": True, **dict(existing)}
                proposal_id, conversation_id, run_id = (str(uuid4()) for _ in range(3))
                now = timestamp()
                content = {
                    "operations": operations,
                    "reason": reason,
                    "before": before,
                    "after": preview["after"],
                    "versions": versions,
                }
                self._save(proposal_id, content)
                repository.insert_task(
                    conn, conversation_id, "记忆整理建议", now, model=source["model"]
                )
                # 已完成的系统提示记录，不调用模型、不伪造用户消息、不计入用户轮数。
                runs.insert(
                    conn,
                    run_id,
                    conversation_id,
                    runs.KIND_EXECUTION_RESULT,
                    {"memory_proposal_id": proposal_id},
                    None,
                    now,
                )
                runs.claim(conn, run_id, now)
                runs.finish(conn, run_id, "done", None, now)
                timeline.insert_notice(conn, conversation_id, run_id, self.question(content))
                conn.execute(
                    "INSERT INTO memory_proposals "
                    "(proposal_id,task_id,fingerprint,status,created_at) "
                    "VALUES (?,?,?,'pending',?)",
                    (proposal_id, conversation_id, fingerprint, now),
                )
            return {
                "changed": False,
                "staged": True,
                "proposal_id": proposal_id,
                "task_id": conversation_id,
            }

    @staticmethod
    def question(content: dict) -> str:
        parts = ["我回顾了近期对话，建议调整以下长期记忆。", f"理由：{content['reason']}"]
        for target, label in (("user", "关于你"), ("memory", "事实与约定")):
            old, new = content["before"][target]["content"], content["after"][target]
            if old != new:
                parts.extend(
                    [f"{label}原内容：\n{old or '（空）'}", f"建议改为：\n{new or '（清空）'}"]
                )
        parts.append("你同意这样修改吗？也可以告诉我哪些要保留、如何调整，或放弃这次建议。")
        return "\n\n".join(parts)

    def resolve(
        self, task_id: str, proposal_id: str, decision: str, operations: list[dict] | None = None
    ) -> dict:
        from server.sessions import timeline

        with self.lock, session(self.path) as conn, write(conn):
            row = conn.execute(
                "SELECT * FROM memory_proposals WHERE proposal_id = ? AND task_id = ?",
                (proposal_id, task_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(proposal_id)
            if row["status"] != "pending":
                raise NotEditableError(row["status"])
            payload = self._load(proposal_id)
            active = conn.execute(
                "SELECT run_id FROM agent_runs WHERE task_id = ? AND kind = 'message' "
                "AND status = 'running' ORDER BY rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if active is None:
                raise invalid("只能在用户亲自发起的对话轮处理候选")
            if decision == "reject":
                result = {"changed": False, "status": "rejected"}
            elif decision == "refresh":
                if not operations:
                    raise invalid("刷新候选需要基于最新记忆提出完整操作，随后再次询问用户")
                preview = self.store.preview(operations)
                payload.update(
                    operations=operations,
                    before=preview["before"],
                    after=preview["after"],
                    versions={t: v["version"] for t, v in preview["before"].items()},
                    refreshed_run_id=active["run_id"],
                )
                identity = json.dumps(
                    [payload["versions"], operations], sort_keys=True, ensure_ascii=False
                )
                fingerprint = hashlib.sha256(identity.encode()).hexdigest()
                duplicate = conn.execute(
                    "SELECT proposal_id FROM memory_proposals WHERE fingerprint = ? "
                    "AND status = 'pending' AND proposal_id != ?",
                    (fingerprint, proposal_id),
                ).fetchone()
                if duplicate is not None:
                    raise invalid("相同的新候选已有待处理对话，请先处理或拒绝那个候选")
                conn.execute(
                    "UPDATE memory_proposals SET refreshed_run_id = ?, fingerprint = ? "
                    "WHERE proposal_id = ?",
                    (active["run_id"], fingerprint, proposal_id),
                )
                self._save(proposal_id, payload)
                question = self.question(payload)
                timeline.insert_notice(conn, task_id, active["run_id"], question)
                return {"changed": False, "staged": True, "question": question}
            elif decision == "apply":
                if (
                    row["refreshed_run_id"] == active["run_id"]
                    or payload.get("refreshed_run_id") == active["run_id"]
                ):
                    raise invalid("新候选尚未获得用户回复，不能在刷新候选的同一轮应用")
                result = self.store.edit(
                    operations if operations is not None else payload["operations"],
                    expected_versions=payload["versions"],
                )
                result["status"] = "applied"
            else:
                raise invalid("decision 必须是 apply、reject 或 refresh")
            conn.execute(
                "UPDATE memory_proposals SET status = ?, resolved_at = ? WHERE proposal_id = ?",
                (result["status"], timestamp(), proposal_id),
            )
            return {**result, "proposal_id": proposal_id}

    def view(self, task_id: str) -> dict:
        with self.lock:
            snapshot = self.store.snapshot()
            result = {
                "changed": False,
                "memory": model_view({t: item["content"] for t, item in snapshot.items()}),
            }
            row = self.for_task(task_id)
            if row is not None:
                result["proposal"] = {**row, **self._load(row["proposal_id"])}
            return result
