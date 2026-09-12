"""HTTP 路由与请求结构：健康检查、任务、对话、SSE 和确认。"""

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server.approval.service import ConfirmationService
from server.config import get_settings
from server.db import schema_version, session
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.tools.gmail.service import ReplyDraftStore


def get_tasks(request: Request) -> SessionStore:
    return request.app.state.tasks


def get_agent(request: Request) -> GatewayRuntime:
    return request.app.state.agent


def get_drafts(request: Request) -> ReplyDraftStore:
    return request.app.state.drafts


def get_confirmations(request: Request) -> ConfirmationService:
    return request.app.state.confirmations


Tasks = Annotated[SessionStore, Depends(get_tasks)]


Agent = Annotated[GatewayRuntime, Depends(get_agent)]


Drafts = Annotated[ReplyDraftStore, Depends(get_drafts)]


Confirmations = Annotated[ConfirmationService, Depends(get_confirmations)]


router = APIRouter()


@router.get("/health", tags=["health"])
def health() -> dict[str, object]:
    settings = get_settings()
    with session() as conn:
        version = schema_version(conn)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"]
    return {
        "status": "ok",
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


@router.get("/tasks/{task_id}/operations", tags=["tasks"])
def task_operations(task_id: str, tasks: Tasks) -> list[dict]:
    return tasks.list_task_operations(task_id)


@router.get("/tasks/{task_id}/history", tags=["tasks"])
async def task_history(task_id: str, agent: Agent) -> dict:
    return await agent.read_history(task_id)


KEEPALIVE_SECONDS = 15


class MessageInput(BaseModel):
    message: str = Field(min_length=1)


@router.post("/tasks/{task_id}/messages", status_code=202, tags=["chat"])
async def submit_message(task_id: str, body: MessageInput, agent: Agent) -> dict:
    return agent.submit_message(task_id, body.message)


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
    return drafts.get_reply_draft(operation_id, version)


@router.patch("/operations/{operation_id}/draft", tags=["approvals"])
def edit_draft(operation_id: str, body: DraftEdit, drafts: Drafts) -> dict:
    return drafts.update_reply_draft(
        operation_id, body.expected_version, list(body.to), body.subject, body.body
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
