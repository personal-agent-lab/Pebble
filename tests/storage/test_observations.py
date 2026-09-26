"""运行观测存储：迁移、级联删除、字段级摘要、步骤幂等与读取口径。"""

import json
import sqlite3

from server import db
from server.config import Settings
from server.db import SCHEMA_VERSION, init_db, schema_version, session, write
from server.sessions import observations
from server.sessions.service import timestamp


def seed_turn(conn: sqlite3.Connection, task_id: str = "t1", run_id: str = "r1") -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, goal, sdk_session_id, created_at, model) "
        "VALUES (?, '目标', NULL, 'now', 'auto')",
        (task_id,),
    )
    conn.execute(
        "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at) "
        "VALUES (?, ?, 'message', '{}', 'running', 'now')",
        (run_id, task_id),
    )


def test_v21_migrates_to_v22(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn, write(conn):
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES (21)")
        for version in range(1, 22):
            for statement in db.SCHEMA_MIGRATIONS[version]:
                conn.execute(statement)
        seed_turn(conn)
    assert init_db() == SCHEMA_VERSION == 22
    with session() as conn:
        assert schema_version(conn) == 22
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert {"run_observations", "observation_steps"} <= tables
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_run_cascades_observations(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        observations.upsert_summary(conn, "r1", materials={"assembled": [], "skipped": []})
        observations.begin_tool_step(
            conn,
            run_id="r1",
            tool_call_id="c1",
            name="gmail_search",
            source="mcp",
            started_at=timestamp(),
        )
        conn.execute("DELETE FROM agent_runs WHERE run_id = 'r1'")
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM observation_steps").fetchone()[0] == 0


def test_upsert_summary_updates_only_given_fields(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        observations.upsert_summary(conn, "r1", materials={"assembled": [{"title": "关于你"}]})
        observations.upsert_summary(conn, "r1", sdk_result={"duration_ms": 12})
        row = conn.execute("SELECT * FROM run_observations WHERE run_id = 'r1'").fetchone()
    assert row["materials"] is not None and "关于你" in row["materials"]
    assert row["sdk_result"] is not None and "12" in row["sdk_result"]
    assert row["context_before"] is None and row["context_after"] is None


def test_tool_step_begin_and_finish_are_idempotent(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        for _ in range(2):
            observations.begin_tool_step(
                conn,
                run_id="r1",
                tool_call_id="c1",
                name="gmail_search",
                source="mcp",
                started_at="2026-01-01T00:00:00",
                item_id="item-1",
            )
        rows = conn.execute("SELECT * FROM observation_steps").fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "running" and rows[0]["item_id"] == "item-1"

        assert observations.finish_tool_step(
            conn, run_id="r1", tool_call_id="c1", status="ok", ended_at="00:00:01", result_chars=42
        )
        # 重复的完成通知不覆盖首次结果。
        assert not observations.finish_tool_step(
            conn, run_id="r1", tool_call_id="c1", status="error", ended_at="00:00:02"
        )
        row = conn.execute("SELECT * FROM observation_steps").fetchone()
    assert row["status"] == "ok" and row["ended_at"] == "00:00:01"
    assert '"chars": 42' in row["detail"]


def test_denied_step_without_begin_lands_terminal(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        observations.complete_tool_step(
            conn,
            run_id="r1",
            tool_call_id="c9",
            name="Read",
            source="builtin",
            status="denied",
            ended_at="00:00:05",
        )
        row = conn.execute("SELECT * FROM observation_steps").fetchone()
    assert row["status"] == "denied" and row["started_at"] is None
    assert row["ended_at"] == "00:00:05"


def test_complete_tool_step_falls_back_when_begin_missing(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        # 开始写入失败后只收到完成通知：仍尽量留下终态。
        observations.complete_tool_step(
            conn,
            run_id="r1",
            tool_call_id="c1",
            name="gmail_search",
            source="mcp",
            status="error",
            ended_at="00:00:03",
            result_chars=7,
        )
        row = conn.execute("SELECT * FROM observation_steps").fetchone()
    assert row["status"] == "error" and '"chars": 7' in row["detail"]


def test_fill_compact_after_backfills_only_missing(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
    observer = observations.TurnObserver("r1")
    observer.record_compact(auto=True, before={"used_percentage": 90.0})
    observer.record_compact(
        auto=False, before={"used_percentage": 85.0}, after={"used_percentage": 30.0}
    )
    with session() as conn, write(conn):
        observations.fill_compact_after(conn, "r1", {"used_percentage": 25.0})
        details = [row["detail"] for row in conn.execute("SELECT detail FROM observation_steps")]
    # 自动压缩缺后占用被回填，手动压缩的实测值不被覆盖。
    assert '"after": {"used_percentage": 25.0}' in details[0]
    assert '"after": {"used_percentage": 30.0}' in details[1]


def test_turn_observer_collect_usage_dedupes(settings: Settings) -> None:
    observer = observations.TurnObserver("r1")

    class Message:
        def __init__(self, usage=None, message_id=None):
            self.usage = usage
            self.message_id = message_id

    # SDK 的 usage 是 TypedDict，运行时就是 dict。
    req1 = {"request_id": "req-1", "input_tokens": 1, "output_tokens": 2, "credits": 0.5}
    observer.collect_usage(Message(req1, "msg-1"))
    observer.collect_usage(Message(req1, "msg-1"))  # 同一请求重发
    observer.collect_usage(Message({"request_id": "req-2", "input_tokens": 3}, None))
    observer.collect_usage(Message(None, None))  # 无标识无用量，不产生条目
    assert [entry["request_id"] for entry in observer._usage] == ["req-1", "req-2"]


def test_record_result_stores_result_usage_and_session_totals(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
    observer = observations.TurnObserver("r1")

    class Result:
        duration_ms = 1200
        duration_api_ms = 800
        num_turns = 3
        is_error = False
        stop_reason = "end_turn"
        usage = {
            "request_id": "req-final",
            "input_tokens": 120,
            "output_tokens": 30,
            "credits": 0.4,
            "context_usage_ratio": 0.106,
        }
        total_credits = 1.5
        model_usage = {
            "qwen-max": {"inputTokens": 100, "outputTokens": 20},
            "qwen-plus": {"inputTokens": 7, "outputTokens": 3},
        }

    observer.record_result(Result())
    with session() as conn:
        stored = conn.execute(
            "SELECT sdk_result FROM run_observations WHERE run_id = 'r1'"
        ).fetchone()["sdk_result"]
    sdk_result = json.loads(stored)
    assert sdk_result["result_usage"] == {
        "request_id": "req-final",
        "input_tokens": 120,
        "output_tokens": 30,
        "credits": 0.4,
        "context_usage_ratio": 0.106,
    }
    # 会话累计快照按模型汇总 token，Credits 取 total_credits。
    assert sdk_result["session_totals"] == {
        "input_tokens": 107,
        "output_tokens": 23,
        "credits": 1.5,
    }


def test_record_result_leaves_snapshot_nulls_when_sdk_omits(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
    observer = observations.TurnObserver("r1")

    class Result:
        is_error = False
        usage = None
        model_usage = None
        total_credits = None

    observer.record_result(Result())
    with session() as conn:
        stored = json.loads(
            conn.execute(
                "SELECT sdk_result FROM run_observations WHERE run_id = 'r1'"
            ).fetchone()["sdk_result"]
        )
    assert stored["result_usage"] is None
    assert stored["session_totals"] == {
        "input_tokens": None,
        "output_tokens": None,
        "credits": None,
    }


def test_usage_totals_requires_identifiable_complete_entries() -> None:
    empty = {"input_tokens": None, "output_tokens": None, "credits": None}
    assert observations.usage_totals(None) == empty
    assert observations.usage_totals([]) == empty
    # 有请求缺字段或无法排除重复时不给合计。
    assert observations.usage_totals([{"message_id": "a", "request_id": None}]) == empty
    assert observations.usage_totals([{"message_id": None, "request_id": None}]) == empty
    totals = observations.usage_totals(
        [
            {
                "message_id": "a",
                "request_id": None,
                "input_tokens": 10,
                "output_tokens": 5,
                "credits": 0.5,
            },
            {
                "message_id": None,
                "request_id": "b",
                "input_tokens": 1,
                "output_tokens": 2,
                "credits": 0.25,
            },
        ]
    )
    assert totals == {"input_tokens": 11, "output_tokens": 7, "credits": 0.75}


def test_read_observations_returns_nulls_and_steps(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        conn.execute(
            "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at, "
            "finished_at) VALUES ('r2', 't1', 'message', '{}', 'done', 'now', 'later')"
        )
        observations.upsert_summary(conn, "r2", sdk_result={"duration_ms": 5, "usage": []})
        observations.begin_tool_step(
            conn,
            run_id="r1",
            tool_call_id="c1",
            name="WebSearch",
            source="builtin",
            started_at="00:00:00",
            item_id="i1",
        )
    runs = observations.read_observations("t1")
    assert [run["run_id"] for run in runs] == ["r1", "r2"]
    first = runs[0]
    assert first["materials"] is None and first["sdk_result"] is None
    assert first["steps"][0]["code"] == "WebSearch" and first["steps"][0]["item_id"] == "i1"
    assert runs[1]["steps"] == []
    assert runs[1]["usage_totals"] == {
        "input_tokens": None,
        "output_tokens": None,
        "credits": None,
    }


def test_read_observations_derives_turn_totals_from_snapshots(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        for run_id in ("r2", "r3", "r4"):
            conn.execute(
                "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at, "
                "finished_at) VALUES (?, 't1', 'message', '{}', 'done', 'now', 'later')",
                (run_id,),
            )
        # 首轮累计快照即本轮消耗；CN 运行时的请求级条目全 0，不能求和。
        observations.upsert_summary(
            conn,
            "r1",
            sdk_result={
                "usage": [{"message_id": "m1", "input_tokens": 0, "output_tokens": 0}],
                "session_totals": {"input_tokens": 100, "output_tokens": 20, "credits": 1.0},
            },
        )
        observations.upsert_summary(
            conn,
            "r2",
            sdk_result={
                "usage": [{"message_id": "m2", "input_tokens": 0, "output_tokens": 0}],
                "session_totals": {"input_tokens": 150, "output_tokens": 32, "credits": None},
            },
        )
        # r3 没有快照（如中断轮）：r4 与 r2 之间无法划界，退回请求级求和。
        observations.upsert_summary(
            conn, "r3", sdk_result={"usage": [{"message_id": "m3", "input_tokens": 0}]}
        )
        observations.upsert_summary(
            conn,
            "r4",
            sdk_result={
                "usage": [],
                "session_totals": {"input_tokens": 200, "output_tokens": 40, "credits": 2.5},
            },
        )
    runs = observations.read_observations("t1")
    assert [run["run_id"] for run in runs] == ["r1", "r2", "r3", "r4"]
    # 首轮：差值就是快照本身。
    assert runs[0]["usage_totals"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "credits": 1.0,
    }
    assert runs[0]["usage_totals_source"] == "delta"
    # 次轮：相邻差值；快照缺 Credits 时该字段留空。
    assert runs[1]["usage_totals"] == {
        "input_tokens": 50,
        "output_tokens": 12,
        "credits": None,
    }
    assert runs[1]["usage_totals_source"] == "delta"
    # r3 自己没有快照，退回请求级求和；条目缺字段，合计留空。
    assert runs[2]["usage_totals_source"] == "requests"
    assert runs[2]["usage_totals"]["input_tokens"] is None
    # r4 的前一轮 r3 缺快照，无法划界，同样退回请求级求和（无条目则无来源）。
    assert runs[3]["usage_totals_source"] is None
    assert runs[3]["usage_totals"] == {
        "input_tokens": None,
        "output_tokens": None,
        "credits": None,
    }


def test_read_observations_ignores_all_zero_snapshot(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        conn.execute(
            "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at, "
            "finished_at) VALUES ('r2', 't1', 'message', '{}', 'done', 'now', 'later')"
        )
        # 实测 CN CLI 的累计层整体为 0：全零快照不是读数，不推算也不显示 0 消耗。
        zero = {"input_tokens": 0, "output_tokens": 0, "credits": 0}
        observations.upsert_summary(conn, "r1", sdk_result={"session_totals": zero})
        observations.upsert_summary(conn, "r2", sdk_result={"session_totals": zero})
    runs = observations.read_observations("t1")
    for run in runs:
        assert run["usage_totals_source"] is None
        assert run["usage_totals"] == {
            "input_tokens": None,
            "output_tokens": None,
            "credits": None,
        }


def test_read_observations_does_not_derive_across_session_reset(settings: Settings) -> None:
    init_db()
    with session() as conn, write(conn):
        seed_turn(conn)
        conn.execute(
            "INSERT INTO agent_runs (run_id, task_id, kind, input, status, created_at, "
            "finished_at) VALUES ('r2', 't1', 'message', '{}', 'done', 'now', 'later')"
        )
        observations.upsert_summary(
            conn,
            "r1",
            sdk_result={
                "session_totals": {"input_tokens": 300, "output_tokens": 60, "credits": 3.0}
            },
        )
        # 会话被重置会让累计读数回落，差值为负时不推算。
        observations.upsert_summary(
            conn,
            "r2",
            sdk_result={
                "session_totals": {"input_tokens": 120, "output_tokens": 25, "credits": 1.2}
            },
        )
    runs = observations.read_observations("t1")
    assert runs[1]["usage_totals"] == {
        "input_tokens": None,
        "output_tokens": None,
        "credits": None,
    }
    assert runs[1]["usage_totals_source"] is None
