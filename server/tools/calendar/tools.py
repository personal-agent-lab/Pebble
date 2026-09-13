"""只向 Agent 暴露本地预览操作，写入由 Confirmation 执行。"""

from server.tools.calendar.service import CalendarDraftStore
from server.tools.registry import SideEffect, tool


@tool(name="calendar_prepare_event", side_effect=SideEffect.LOCAL_WRITE)
def prepare_event(
    task_id: str,
    request_id: str,
    title: str,
    start: str,
    end: str,
    timezone: str = "",
    location: str = "",
    description: str = "",
) -> dict:
    """保存单次定时日程预览，不创建。start/end 必须明确。

    timezone 为 IANA 时区，留空使用服务端默认。
    相同请求重试复用 request_id；修改必须使用更新工具。
    """
    return CalendarDraftStore().prepare(
        task_id,
        request_id,
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
        description=description,
    )


@tool(name="calendar_read_event_draft", side_effect=SideEffect.READONLY)
def read_event_draft(task_id: str, operation_id: str) -> dict:
    """读取当前任务的最新日程预览和版本。"""
    store = CalendarDraftStore()
    store.require_task(task_id, operation_id)
    return store.get(operation_id)


@tool(name="calendar_update_event_draft", side_effect=SideEffect.LOCAL_WRITE)
def update_event_draft(
    task_id: str,
    operation_id: str,
    expected_version: int,
    title: str,
    start: str,
    end: str,
    timezone: str,
    location: str = "",
    description: str = "",
) -> dict:
    """按刚读取的版本保存完整日程新预览，不创建。"""
    store = CalendarDraftStore()
    store.require_task(task_id, operation_id)
    return store.update(
        operation_id,
        expected_version,
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        location=location,
        description=description,
    )
