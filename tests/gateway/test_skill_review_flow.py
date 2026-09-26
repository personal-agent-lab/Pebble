"""真实 GatewayRuntime 的跨任务 Skill 复盘完成钩子。"""

import asyncio

import pytest

from server.db import init_db, session
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.skills.review import SkillReviewScheduler
from server.skills.service import SkillService
from tests.support.agent_double import FakeAgentGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def settle(service: GatewayRuntime) -> None:
    async with asyncio.timeout(5):
        while service._active or service._skill_review_task is not None:
            tasks = [*service._active.values()]
            if service._skill_review_task is not None:
                tasks.append(service._skill_review_task)
            await asyncio.gather(*tasks)


async def test_two_tasks_trigger_one_invisible_background_review(settings):
    init_db()
    skills = SkillService(settings.data_dir, settings.db_path)
    scheduler = SkillReviewScheduler(skills, path=settings.db_path, interval=2)
    gateway = FakeAgentGateway()
    service = GatewayRuntime(gateway, skill_reviews=scheduler, path=settings.db_path)
    tasks = SessionStore(settings.db_path)
    try:
        first = tasks.create_task("第一项")["task_id"]
        second = tasks.create_task("第二项")["task_id"]
        service.submit_message(first, "先做第一项")
        await settle(service)
        assert gateway.skill_review_calls == []
        service.submit_message(second, "第二项纠正做法")
        await settle(service)
        assert len(gateway.skill_review_calls) == 1
        assert "第二项纠正做法" in gateway.skill_review_calls[0]["material"]
        with session(settings.db_path) as conn:
            row = conn.execute("SELECT status FROM skill_reviews").fetchone()
            assert row["status"] == "completed"
            for task_id in (first, second):
                kinds = [
                    item[0]
                    for item in conn.execute(
                        "SELECT kind FROM task_timeline_items WHERE task_id=?", (task_id,)
                    )
                ]
                assert kinds == ["text", "text"]
    finally:
        await service.close()
