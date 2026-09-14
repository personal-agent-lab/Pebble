"""Agent 可见的日历查询与本地预览工具。"""

from typing import Protocol

from server.tools.calendar.service import PRIMARY_CALENDAR, CalendarPreviewStore
from server.tools.registry import SideEffect, tool


class CalendarReader(Protocol):
    def list_events(
        self, time_min: str, time_max: str, calendar_id: str, max_results: int
    ) -> dict: ...
    def get_event(self, event_id: str) -> dict: ...
    def check_conflicts(self, start: str, end: str, calendar_id: str) -> dict: ...


@tool(name="calendar_list_events", side_effect=SideEffect.READONLY)
def list_events(
    time_min: str,
    time_max: str,
    calendar_id: str = PRIMARY_CALENDAR,
    max_results: int = 50,
    *,
    calendar: CalendarReader,
) -> dict:
    """查询主日历在指定时间范围内的事件。时间必须包含时区偏移。"""
    return calendar.list_events(time_min, time_max, calendar_id, max_results)


@tool(name="calendar_get_event", side_effect=SideEffect.READONLY)
def get_event(event_id: str, *, calendar: CalendarReader) -> dict:
    """按 event_id 读取主日历中的单个事件完整内容。"""
    return calendar.get_event(event_id)


@tool(name="calendar_check_conflicts", side_effect=SideEffect.READONLY)
def check_conflicts(
    start: str, end: str, calendar_id: str = PRIMARY_CALENDAR, *, calendar: CalendarReader
) -> dict:
    """查询目标时间内的忙碌区间与冲突事件；不替用户作结论。"""
    return calendar.check_conflicts(start, end, calendar_id)


@tool(name="calendar_prepare_event", side_effect=SideEffect.LOCAL_WRITE, emits_draft_saved=True)
def prepare_event(
    summary: str,
    start: str,
    end: str,
    all_day: bool = False,
    location: str | None = None,
    description: str = "",
    calendar_id: str = PRIMARY_CALENDAR,
    *,
    task_id: str,
    calendar_previews: CalendarPreviewStore,
) -> dict:
    """保存待确认的单次日程预览，不会在 iCloud 创建事件。"""
    return calendar_previews.save_preview(
        task_id,
        summary=summary,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        description=description,
        calendar_id=calendar_id,
    )


@tool(name="calendar_read_preview", side_effect=SideEffect.READONLY)
def read_preview(
    operation_id: str, *, task_id: str, calendar_previews: CalendarPreviewStore
) -> dict:
    """读取当前任务关联的日程预览。"""
    calendar_previews.require_task(task_id, operation_id)
    return calendar_previews.get_preview(operation_id)


@tool(name="calendar_update_preview", side_effect=SideEffect.LOCAL_WRITE, emits_draft_saved=True)
def update_preview(
    operation_id: str,
    expected_version: int,
    summary: str,
    start: str,
    end: str,
    all_day: bool = False,
    location: str | None = None,
    description: str = "",
    calendar_id: str = PRIMARY_CALENDAR,
    *,
    task_id: str,
    calendar_previews: CalendarPreviewStore,
) -> dict:
    """用完整字段保存当前任务中日程预览的新版本，不会创建事件。"""
    calendar_previews.require_task(task_id, operation_id)
    return calendar_previews.update_preview(
        operation_id,
        expected_version,
        summary=summary,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        description=description,
        calendar_id=calendar_id,
    )
