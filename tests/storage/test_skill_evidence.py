import pytest

from server.db import init_db, session, write
from server.errors import SkillValidationError
from server.sessions.service import SessionStore
from server.skills import evidence
from server.skills.runtime import Scope, current
from server.skills.tools import skill_propose


def test_verified_sequences_and_proposal_dedup(settings):
    init_db()
    tasks = []
    for i in range(3):
        task = SessionStore().create_task(f"task {i}")["task_id"]
        tasks.append(task)
        run = f"run_{i}"
        with session() as conn, write(conn):
            conn.execute(
                "INSERT INTO agent_runs(run_id,task_id,kind,input,status,created_at,finished_at) "
                "VALUES (?,?,'message','{}','done','now','now')",
                (run, task),
            )
        token = current.set(Scope(run, set(), True, set()))
        try:
            evidence.record("lookup", {"secret": "never persist this value"}, True)
            evidence.record("check", {}, True)
        finally:
            current.reset(token)
    result = evidence.verify({"tasks": tasks, "occurrences": 999})
    assert result["occurrences"] == 3
    with session() as conn:
        assert "never persist" not in str(
            [dict(r) for r in conn.execute("SELECT * FROM skill_tool_evidence")]
        )
    first = skill_propose("Reusable", "Lookup and check", "Parameterized workflow", evidence=result)
    second = skill_propose(
        "Reusable", "Lookup and check", "Parameterized workflow", evidence=result
    )
    assert first["draft_id"] == second["draft_id"]
    with pytest.raises(SkillValidationError):
        evidence.verify({"tasks": tasks[:2], "occurrences": 100})
    with session() as conn, write(conn):
        conn.execute("UPDATE skill_tool_evidence SET succeeded = 0 WHERE run_id = 'run_0'")
    with pytest.raises(SkillValidationError):
        evidence.verify({"tasks": tasks})
