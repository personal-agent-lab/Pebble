"""HTTP 路由与请求结构：健康检查、任务、对话、SSE、确认与资料管理。"""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from server.approval.service import ConfirmationService
from server.config import get_settings
from server.db import schema_version, session
from server.errors import DependencyUnavailableError
from server.gateway.runtime import GatewayRuntime
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore


def get_tasks(request: Request) -> SessionStore:
    return request.app.state.tasks


def get_agent(request: Request) -> GatewayRuntime:
    return request.app.state.agent


def get_drafts(request: Request) -> MailDraftStore:
    return request.app.state.drafts


def get_confirmations(request: Request) -> ConfirmationService:
    return request.app.state.confirmations


def get_kb(request: Request) -> KbStore:
    kb_store = request.app.state.kb_store
    if kb_store is None:
        raise DependencyUnavailableError("资料库未接入")
    return kb_store


Tasks = Annotated[SessionStore, Depends(get_tasks)]


Agent = Annotated[GatewayRuntime, Depends(get_agent)]


Drafts = Annotated[MailDraftStore, Depends(get_drafts)]


Confirmations = Annotated[ConfirmationService, Depends(get_confirmations)]


Kb = Annotated[KbStore, Depends(get_kb)]


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


class MessageInput(BaseModel):
    message: str = Field(min_length=1)
    target: MessageTarget | None = None


@router.post("/tasks/{task_id}/messages", status_code=202, tags=["chat"])
async def submit_message(task_id: str, body: MessageInput, agent: Agent) -> dict:
    return agent.submit_message(
        task_id,
        body.message,
        target=body.target.model_dump() if body.target is not None else None,
    )


@router.post("/tasks/{task_id}/memory-review", status_code=202, tags=["chat"])
async def trigger_memory_review(task_id: str, tasks: Tasks, agent: Agent) -> dict:
    """手动登记一次后台记忆回顾：立即覆盖整个任务，不受周期间隔限制。"""
    tasks.get_task(task_id)
    return agent.submit_memory_review(task_id)


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


# ---------- 资料管理 ----------
#
# 界面操作由用户本人发起，等同于直接改文件：不经过 Agent，也不需要对话中的同意；
# 写入沿用资料库自身的版本校验，保存、移动与删除都要带上读取时的 version。


class KbDocumentCreate(BaseModel):
    title: str
    body: str
    summary: str | None = None
    path: str | None = None
    tags: list[str] | None = None


class KbDocumentUpdate(BaseModel):
    path: str
    expected_version: str = Field(min_length=1)
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    summary: str | None = None


class KbDocumentMove(BaseModel):
    path: str
    expected_version: str = Field(min_length=1)
    new_path: str


class KbDocumentDelete(BaseModel):
    path: str
    expected_version: str = Field(min_length=1)


@router.get("/kb/documents", tags=["kb"])
def kb_documents(kb: Kb, directory: str | None = None) -> dict:
    return kb.list(directory=directory)


@router.get("/kb/document", tags=["kb"])
def kb_document(kb: Kb, path: str) -> dict:
    document = kb.read(path=path)
    return {
        "id": document["id"],
        "path": document["path"],
        "title": document["title"],
        "tags": document["tags"] or [],
        "summary": document["summary"] or "",
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "version": document["commit"],
        "body": document["body"],
    }


@router.get("/kb/search", tags=["kb"])
def kb_search(kb: Kb, q: str, tag: str | None = None) -> dict:
    return kb.search(query=q, tag=tag, max_results=20)


@router.post("/kb/documents", status_code=201, tags=["kb"])
def kb_create(body: KbDocumentCreate, kb: Kb) -> dict:
    return kb.save(
        title=body.title, body=body.body, path=body.path, tags=body.tags, summary=body.summary
    )


@router.post("/kb/document/update", tags=["kb"])
def kb_update(body: KbDocumentUpdate, kb: Kb) -> dict:
    return kb.update(
        expected_version=body.expected_version,
        path=body.path,
        title=body.title,
        body=body.body,
        tags=body.tags,
        summary=body.summary,
    )


@router.post("/kb/document/move", tags=["kb"])
def kb_move(body: KbDocumentMove, kb: Kb) -> dict:
    return kb.move(expected_version=body.expected_version, path=body.path, new_path=body.new_path)


@router.post("/kb/document/delete", tags=["kb"])
def kb_delete(body: KbDocumentDelete, kb: Kb) -> dict:
    return kb.delete(expected_version=body.expected_version, path=body.path)
