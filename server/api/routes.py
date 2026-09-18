"""HTTP 路由与请求结构：健康检查、任务、对话、SSE、确认、记忆与资料管理。"""

import asyncio
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from server.agent.models import ModelCatalog
from server.approval.service import ConfirmationService
from server.attachments import AttachmentStore, prepare_uploads
from server.config import get_settings
from server.db import schema_version, session
from server.errors import AttachmentValidationError, DependencyUnavailableError, KbValidationError
from server.gateway.runtime import GatewayRuntime
from server.memory.service import MemoryStore
from server.sessions.history import HistoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.assets import ASSETS_DIR, MAX_ASSET_SIZE, describe, sniff
from server.tools.personal_kb.service import KbStore
from server.tools.personal_kb.summary import generate_summary


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


def get_memory(request: Request) -> MemoryStore:
    return request.app.state.memory_store


Memory = Annotated[MemoryStore, Depends(get_memory)]


def get_history(request: Request) -> HistoryStore:
    return request.app.state.history


History = Annotated[HistoryStore, Depends(get_history)]


def get_models(request: Request) -> ModelCatalog:
    return request.app.state.model_catalog


def get_attachments(request: Request) -> AttachmentStore:
    return request.app.state.attachments


Models = Annotated[ModelCatalog, Depends(get_models)]
Attachments = Annotated[AttachmentStore, Depends(get_attachments)]


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
        # 未配置的服务不算故障：状态仍为 ok，只在这里说明哪些功能关闭了。
        "services": request.app.state.services,
        "data_dir": str(settings.data_dir),
        "schema_version": version,
        "journal_mode": journal_mode,
    }


@router.get("/tasks", tags=["tasks"])
def list_tasks(tasks: Tasks) -> list[dict]:
    return tasks.list_tasks()


@router.get("/models", tags=["chat"])
async def list_models(models: Models) -> dict:
    return await models.response()


@router.post("/tasks", status_code=201, tags=["tasks"])
async def create_task(
    agent: Agent,
    models: Models,
    response: Response,
    model: Annotated[str, Form()],
    message: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
    task_id: Annotated[UUID | None, Form()] = None,
) -> dict:
    """新建任务；客户端可自带 `task_id` 先行展示，重复提交同一标识返回已创建的任务（200）。"""
    client_id = str(task_id) if task_id is not None else None
    if client_id is not None and (existing := agent.started_task(client_id)) is not None:
        response.status_code = 200
        return existing
    prepared = await prepare_uploads(files or [])
    if not message.strip() and not prepared:
        raise AttachmentValidationError(
            [{"field": "message", "message": "请输入文字或至少添加一个附件"}]
        )
    return agent.start_task(await models.validate(model), message, prepared, client_id)


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


@router.post("/tasks/{task_id}/messages", status_code=202, tags=["chat"])
async def submit_message(
    task_id: str,
    agent: Agent,
    message: Annotated[str, Form()] = "",
    target: Annotated[str | None, Form()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> dict:
    prepared = await prepare_uploads(files or [])
    if not message.strip() and not prepared:
        raise AttachmentValidationError(
            [{"field": "message", "message": "请输入文字或至少添加一个附件"}]
        )
    parsed_target = None
    if target is not None:
        try:
            parsed_target = MessageTarget.model_validate_json(target).model_dump()
        except ValueError as error:
            raise AttachmentValidationError(
                [{"field": "target", "message": "消息目标不合法"}]
            ) from error
    return agent.submit_message(
        task_id,
        message,
        target=parsed_target,
        attachments=prepared,
    )


@router.post("/tasks/{task_id}/retry", status_code=202, tags=["chat"])
async def retry_last_message(task_id: str, agent: Agent) -> dict:
    """只重试任务最后一轮已中断的用户消息，不新增时间线消息。"""
    return agent.retry_last_message(task_id)


@router.get("/tasks/{task_id}/attachments/{file_id}", tags=["chat"])
def read_attachment(task_id: str, file_id: str, attachments: Attachments) -> FileResponse:
    with session() as conn:
        record, path = attachments.get(conn, task_id, file_id)
    inline = record["mime_type"].startswith("image/")
    return FileResponse(
        path,
        media_type=record["mime_type"],
        filename=None if inline else record["filename"],
        content_disposition_type="inline" if inline else "attachment",
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
    那不是用户授权检查，与本接口不同是有意的。单用户实例里草稿都属于同一用户，
    用户身份与请求来源由访问控制（`server/api/access.py`）统一校验。
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


@router.post("/tasks/{task_id}/cancellations", tags=["approvals"])
def cancel(task_id: str, body: ConfirmationInput, confirmations: Confirmations) -> dict:
    """取消待确认的草稿：不发送，也不再能编辑或确认。"""
    return confirmations.cancel(task_id, body.operation_id, body.version)


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


# ---------- 历史对话检索 ----------


@router.get("/history/search", tags=["history"])
def history_search(
    history: History,
    q: str,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> dict:
    """页面上的历史搜索：与 Agent 的 history_search 同一套检索，不排除任何任务。"""
    return history.search(q, after=after, before=before, max_results=limit)


# ---------- 资料管理 ----------
#
# 界面操作由用户本人发起，等同于直接改文件：不经过 Agent，也不需要对话中的同意；
# 写入沿用资料库自身的版本校验，保存、移动与删除都要带上读取时的 version。


class MemoryDocumentWrite(BaseModel):
    content: str
    expected_version: str = Field(min_length=1)


def memory_section(result: dict) -> dict:
    return {
        "content": result["content"],
        "usage": result["usage"],
        "version": result["version"],
    }


@router.get("/memory", tags=["memory"])
def memory_current(memory: Memory) -> dict:
    """两块长期记忆的当前全文、容量与版本；超出容量的文件照常返回。"""
    return {target: memory_section(section) for target, section in memory.snapshot().items()}


@router.put("/memory/{target}", tags=["memory"])
def memory_write(target: str, body: MemoryDocumentWrite, memory: Memory) -> dict:
    """整份保存一块记忆：读取之后文件被改过时返回 409，不覆盖；未知分区返回 invalid_memory。"""
    result = memory.write(target, body.content, expected_version=body.expected_version)
    return {"target": target, "changed": result["changed"], **memory_section(result)}


class KbDocumentCreate(BaseModel):
    title: str
    body: str
    summary: str | None = None
    path: str | None = None
    directory: str | None = None


class KbFolderCreate(BaseModel):
    path: str


class KbSummaryDraft(BaseModel):
    title: str = ""
    body: str


class KbDocumentUpdate(BaseModel):
    path: str
    expected_version: str = Field(min_length=1)
    title: str | None = None
    body: str | None = None
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
        "summary": document["summary"] or "",
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "version": document["commit"],
        "body": document["body"],
    }


@router.get("/kb/search", tags=["kb"])
def kb_search(kb: Kb, q: str) -> dict:
    return kb.search(query=q, max_results=20)


@router.post("/kb/documents", status_code=201, tags=["kb"])
def kb_create(body: KbDocumentCreate, kb: Kb) -> dict:
    return kb.save(
        title=body.title,
        body=body.body,
        path=body.path,
        directory=body.directory,
        summary=body.summary,
    )


@router.get("/kb/folders", tags=["kb"])
def kb_folders(kb: Kb) -> dict:
    return {"folders": kb.folders()}


@router.post("/kb/folders", status_code=201, tags=["kb"])
def kb_create_folder(body: KbFolderCreate, kb: Kb) -> dict:
    return kb.create_folder(body.path)


@router.post("/kb/summary", tags=["kb"])
async def kb_summary(body: KbSummaryDraft, agent: Agent) -> dict:
    return {"summary": await generate_summary(agent.gateway, body.title, body.body)}


@router.post("/kb/assets", status_code=201, tags=["kb"])
async def kb_upload_asset(kb: Kb, file: Annotated[UploadFile, File()]) -> dict:
    """保存正文图片并立即返回路径；说明另由 `/kb/assets/describe` 生成，不拖慢插入。"""
    data = await file.read(MAX_ASSET_SIZE + 1)
    return await run_in_threadpool(kb.save_asset, data)


class KbAssetDescribe(BaseModel):
    path: str


@router.post("/kb/assets/describe", tags=["kb"])
async def kb_describe_asset(body: KbAssetDescribe, kb: Kb, agent: Agent) -> dict:
    """由轻量模型看图生成说明，不写入任何文件；模型看不到图或调用失败时为空串。"""
    prefix = f"{ASSETS_DIR}/"
    if not body.path.startswith(prefix):
        raise KbValidationError([{"field": "path", "message": "只能为资料库里的图片生成说明"}])
    path, _ = kb.asset(body.path[len(prefix) :])
    data = await run_in_threadpool(path.read_bytes)
    return {"description": await describe(agent.gateway, data, sniff(data) or "png")}


@router.get("/kb/assets/{name}", tags=["kb"])
def kb_read_asset(kb: Kb, name: str) -> FileResponse:
    path, mime_type = kb.asset(name)
    return FileResponse(path, media_type=mime_type, headers={"X-Content-Type-Options": "nosniff"})


@router.post("/kb/document/update", tags=["kb"])
def kb_update(body: KbDocumentUpdate, kb: Kb) -> dict:
    return kb.update(
        expected_version=body.expected_version,
        path=body.path,
        title=body.title,
        body=body.body,
        summary=body.summary,
    )


@router.post("/kb/document/move", tags=["kb"])
def kb_move(body: KbDocumentMove, kb: Kb) -> dict:
    return kb.move(expected_version=body.expected_version, path=body.path, new_path=body.new_path)


@router.post("/kb/document/delete", tags=["kb"])
def kb_delete(body: KbDocumentDelete, kb: Kb) -> dict:
    return kb.delete(expected_version=body.expected_version, path=body.path)
