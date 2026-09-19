"""HTTP 路由与请求结构：健康检查、任务、对话、SSE、确认和 Skill。"""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server.approval.service import ConfirmationService
from server.config import get_settings
from server.db import schema_version, session
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.skills import service as skill_service
from server.skills.models import SkillStatus
from server.tools.gmail.service import MailDraftStore


def get_tasks(request: Request) -> SessionStore:
    return request.app.state.tasks


def get_agent(request: Request) -> GatewayRuntime:
    return request.app.state.agent


def get_drafts(request: Request) -> MailDraftStore:
    return request.app.state.drafts


def get_confirmations(request: Request) -> ConfirmationService:
    return request.app.state.confirmations


Tasks = Annotated[SessionStore, Depends(get_tasks)]


Agent = Annotated[GatewayRuntime, Depends(get_agent)]


Drafts = Annotated[MailDraftStore, Depends(get_drafts)]


Confirmations = Annotated[ConfirmationService, Depends(get_confirmations)]


router = APIRouter()


@router.get("/health", tags=["health"])
def health(request: Request) -> dict[str, object]:
    settings = get_settings()
    with session() as conn:
        version = schema_version(conn)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"]
    mail_source = request.app.state.mail_source
    mail_source_error = mail_source.error if mail_source is not None else None
    return {
        "status": "degraded" if mail_source_error else "ok",
        "mail_source_error": mail_source_error,
        "data_dir": str(settings.data_dir),
        "schema_version": version,
        "journal_mode": journal_mode,
    }


class TaskCreate(BaseModel):
    goal: str = Field(min_length=1)


@router.get("/tasks", tags=["tasks"])
def list_tasks(tasks: Tasks) -> list[dict]:
    return tasks.list_tasks()


@router.post("/tasks", status_code=201, tags=["tasks"])
def create_task(body: TaskCreate, tasks: Tasks) -> dict:
    return tasks.create_task(body.goal)


@router.get("/tasks/{task_id}", tags=["tasks"])
def task_detail(task_id: str, tasks: Tasks, agent: Agent) -> dict:
    record = tasks.get_task(task_id)
    record["latest_run"] = agent.latest_run(task_id)
    return record


@router.delete("/tasks/{task_id}", status_code=204, tags=["tasks"])
def delete_task(task_id: str, tasks: Tasks) -> None:
    tasks.delete_task(task_id)


@router.get("/tasks/{task_id}/operations", tags=["tasks"])
def task_operations(task_id: str, tasks: Tasks) -> list[dict]:
    return tasks.list_task_operations(task_id)


@router.get("/tasks/{task_id}/timeline", tags=["tasks"])
def task_timeline(task_id: str, agent: Agent) -> dict:
    return agent.get_timeline(task_id)


KEEPALIVE_SECONDS = 15


class MessageTarget(BaseModel):
    kind: Literal["mail_draft"]
    operation_id: str


class SkillRef(BaseModel):
    id: str
    revision: str = Field(min_length=1)


class MessageInput(BaseModel):
    message: str = Field(min_length=1)
    target: MessageTarget | None = None
    skills: list[SkillRef] | None = None
    excluded_skill_ids: list[str] | None = None
    auto_match_skills: bool = True


@router.post("/tasks/{task_id}/messages", status_code=202, tags=["chat"])
async def submit_message(task_id: str, body: MessageInput, agent: Agent) -> dict:
    return agent.submit_message(
        task_id,
        body.message,
        target=body.target.model_dump() if body.target is not None else None,
        skill_ids=[s.id for s in (body.skills or [])],
        skill_refs=[s.model_dump() for s in (body.skills or [])],
        excluded_skill_ids=body.excluded_skill_ids or [],
        auto_match_skills=body.auto_match_skills,
    )


def event_frame(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {event['type']}\ndata: {data}\n\n"


@router.get("/tasks/{task_id}/events", tags=["chat"])
async def task_events(
    task_id: str, request: Request, tasks: Tasks, agent: Agent
) -> StreamingResponse:
    tasks.get_task(task_id)
    subscription = agent.events.subscribe(task_id)

    async def frames():
        # 用异步生成器：页面断开时能及时退订；同步生成器在线程池里不会收到关闭信号。
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(subscription.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keepalive\n\n"
                    continue
                yield event_frame(event)
        finally:
            agent.events.unsubscribe(task_id, subscription)

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


class DraftEdit(BaseModel):
    expected_version: int
    to: list[str]
    subject: str
    body: str


class ConfirmationInput(BaseModel):
    operation_id: str
    version: int


@router.get("/operations/{operation_id}/draft", tags=["approvals"])
def read_draft(operation_id: str, drafts: Drafts, version: int | None = None) -> dict:
    return drafts.get_draft(operation_id, version)


@router.patch("/operations/{operation_id}/draft", tags=["approvals"])
def edit_draft(operation_id: str, body: DraftEdit, drafts: Drafts) -> dict:
    """按邮件契约编辑草稿：输入不含任务标识，同一操作可由任何关联任务的页面编辑。

    工具路径另有归属检查，限制模型只能读写本会话任务的草稿，防邮件正文里的指令越界；
    那不是用户授权检查，与本接口不同是有意的。用户身份检查随正式 Web 接入一起补。
    """
    return drafts.update_draft(
        operation_id,
        body.expected_version,
        list(body.to),
        body.subject,
        body.body,
    )


@router.post("/tasks/{task_id}/confirmations", status_code=202, tags=["approvals"])
async def confirm(
    task_id: str, body: ConfirmationInput, confirmations: Confirmations, agent: Agent
) -> dict:
    view = confirmations.accept_confirmation(task_id, body.operation_id, body.version)
    agent.confirm_execution(body.operation_id)
    return view


@router.get("/operations/{operation_id}/execution", tags=["approvals"])
def execution(operation_id: str, confirmations: Confirmations) -> dict:
    return confirmations.get_execution(operation_id)


@router.post("/operations/{operation_id}/verification", tags=["approvals"])
def verify(operation_id: str, confirmations: Confirmations, agent: Agent) -> dict:
    """核实待核实的发送结果：只读查询实际结果，不重发（邮件契约 §4）。

    读接口不做外部调用，核实要用户或恢复流程显式发起。升级为 sent 时会登记结果回传，
    这里接着推进，让原会话拿到最终结果。
    """
    view = confirmations.verify_pending(operation_id)
    agent.kick()
    return view


# ---------- Skill 路由 ----------


class SkillCreate(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = ""
    triggers: list[str] | None = None
    inputs: list[dict] | None = None
    tools: list[str] | None = None
    side_effects: list[str] | None = None
    requires_confirmation: bool = False


class SkillUpdate(BaseModel):
    expected_revision: str
    name: str | None = None
    description: str | None = None
    body: str | None = None
    triggers: list[str] | None = None
    inputs: list[dict] | None = None
    tools: list[str] | None = None
    side_effects: list[str] | None = None
    requires_confirmation: bool | None = None


class SkillDraftUpdate(BaseModel):
    expected_revision: str
    name: str | None = None
    description: str | None = None
    body: str | None = None
    triggers: list[str] | None = None
    inputs: list[dict] | None = None
    tools: list[str] | None = None
    side_effects: list[str] | None = None
    requires_confirmation: bool | None = None


@router.get("/skills", tags=["skills"])
def list_skills(status: SkillStatus | None = None, search: str | None = None) -> list[dict]:
    items = skill_service.list_skills(status=status, search=search)
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "status": s.status.value,
            "source": s.source.value,
            "content_hash": s.content_hash,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in items
    ]


@router.post("/skills", status_code=201, tags=["skills"])
def create_skill(body: SkillCreate) -> dict:
    skill = skill_service.create_skill(
        id=body.id,
        name=body.name,
        description=body.description,
        body=body.body,
        triggers=body.triggers,
        inputs=body.inputs,
        tools=body.tools,
        side_effects=body.side_effects,
        requires_confirmation=body.requires_confirmation,
    )
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "status": skill.status.value,
        "content_hash": skill.content_hash,
    }


@router.get("/skills/{skill_id}", tags=["skills"])
def get_skill(skill_id: str) -> dict:
    skill = skill_service.get_skill(skill_id)
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "status": skill.status.value,
        "source": skill.source.value,
        "body": skill.body,
        "triggers": skill.triggers,
        "inputs": [{"name": i.name, "required": i.required, "note": i.note} for i in skill.inputs],
        "tools": skill.tools,
        "side_effects": skill.side_effects,
        "requires_confirmation": skill.requires_confirmation,
        "content_hash": skill.content_hash,
        "approved_version": skill.approved_version,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
    }


@router.patch("/skills/{skill_id}", tags=["skills"])
def update_skill(skill_id: str, body: SkillUpdate) -> dict:
    skill = skill_service.update_skill(
        skill_id,
        body.expected_revision,
        name=body.name,
        description=body.description,
        body=body.body,
        triggers=body.triggers,
        inputs=body.inputs,
        tools=body.tools,
        side_effects=body.side_effects,
        requires_confirmation=body.requires_confirmation,
    )
    return {
        "id": skill.id,
        "name": skill.name,
        "content_hash": skill.content_hash,
    }


@router.post("/skills/{skill_id}/disable", tags=["skills"])
def disable_skill(skill_id: str) -> dict:
    skill = skill_service.disable_skill(skill_id)
    return {"id": skill.id, "status": skill.status.value}


@router.post("/skills/{skill_id}/enable", tags=["skills"])
def enable_skill(skill_id: str) -> dict:
    skill = skill_service.enable_skill(skill_id)
    return {"id": skill.id, "status": skill.status.value}


@router.post("/skills/{skill_id}/archive", status_code=204, tags=["skills"])
def archive_skill(skill_id: str) -> None:
    skill_service.archive_skill(skill_id)


@router.get("/skills/{skill_id}/versions", tags=["skills"])
def get_skill_versions(skill_id: str) -> list[dict]:
    versions = skill_service.get_versions(skill_id)
    return [
        {
            "skill_id": v.skill_id,
            "revision": v.revision,
            "body": v.snapshot.body,
            "approved_at": v.approved_at,
            "created_at": v.created_at,
        }
        for v in versions
    ]


@router.post("/skills/{skill_id}/restore", tags=["skills"])
def restore_skill_version(skill_id: str, revision: str) -> dict:
    draft = skill_service.restore_version(skill_id, revision)
    return {
        "draft_id": draft.draft_id,
        "skill_id": draft.skill_id,
        "base_revision": draft.base_revision,
    }


@router.get("/skill-drafts/{draft_id}", tags=["skills"])
def get_skill_draft(draft_id: str) -> dict:
    from server.skills import repository

    draft = repository.load_draft(draft_id)
    if draft is None:
        from server.errors import NotFoundError

        raise NotFoundError(f"草稿不存在: {draft_id}")
    return {
        "draft_id": draft.draft_id,
        "evidence": __import__("dataclasses").asdict(draft.skill.evidence)
        if draft.skill.evidence
        else None,
        "skill_id": draft.skill_id,
        "base_revision": draft.base_revision,
        "name": draft.skill.name,
        "description": draft.skill.description,
        "body": draft.skill.body,
        "triggers": draft.skill.triggers,
        "inputs": [
            {"name": i.name, "required": i.required, "note": i.note} for i in draft.skill.inputs
        ],
        "tools": draft.skill.tools,
        "side_effects": draft.skill.side_effects,
        "requires_confirmation": draft.skill.requires_confirmation,
        "content_hash": draft.skill.content_hash,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


@router.patch("/skill-drafts/{draft_id}", tags=["skills"])
def update_skill_draft(draft_id: str, body: SkillDraftUpdate) -> dict:
    draft = skill_service.update_draft(
        draft_id,
        body.expected_revision,
        name=body.name,
        description=body.description,
        body=body.body,
        triggers=body.triggers,
        inputs=body.inputs,
        tools=body.tools,
        side_effects=body.side_effects,
        requires_confirmation=body.requires_confirmation,
    )
    return {
        "draft_id": draft.draft_id,
        "content_hash": draft.skill.content_hash,
    }


@router.post("/skill-drafts/{draft_id}/approve", tags=["skills"])
def approve_skill_draft(draft_id: str, body: SkillUpdate) -> dict:
    skill = skill_service.approve_draft(draft_id, body.expected_revision)
    return {
        "id": skill.id,
        "name": skill.name,
        "status": skill.status.value,
        "content_hash": skill.content_hash,
    }


@router.post("/skill-drafts/{draft_id}/reject", status_code=204, tags=["skills"])
def reject_skill_draft(draft_id: str) -> None:
    skill_service.reject_draft(draft_id)


@router.get("/skill-drafts", tags=["skills"])
def list_skill_drafts() -> list[dict]:
    from server.skills.repository import list_drafts

    return [get_skill_draft(d.draft_id) for d in list_drafts()]


@router.get("/tasks/{task_id}/skill-usage", tags=["skills"])
def skill_usage(task_id: str, tasks: Tasks) -> list[dict]:
    from server.db import session

    tasks.get_task(task_id)
    with session(tasks.path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT l.* FROM skill_run_links l JOIN agent_runs r USING(run_id) "
                "WHERE r.task_id = ? ORDER BY loaded_at",
                (task_id,),
            )
        ]
