"""Agent 可见的日历查询与直连创建工具。"""

from typing import Protocol

from server.tools.calendar.service import PRIMARY_CALENDAR, CalendarEventStore, validate_event
from server.tools.registry import SideEffect, activity, tool


class CalendarReader(Protocol):
    def list_events(
        self, time_min: str, time_max: str, calendar_id: str, max_results: int
    ) -> dict: ...
    def get_event(self, event_id: str) -> dict: ...
    def check_conflicts(self, start: str, end: str, calendar_id: str) -> dict: ...
    def conflict_window(self, fields: dict) -> tuple[str, str]: ...


class EventCreator(Protocol):
    """确认服务中日程直连创建所需的能力。"""

    def accept_confirmation(self, task_id: str, operation_id: str, version: int) -> dict: ...
    def execute_accepted(self, operation_id: str, *, deliver: bool = True) -> dict | None: ...
    def get_execution(self, operation_id: str) -> dict: ...


def _span(args: dict, start: str, end: str) -> str | None:
    """步骤说明里的时间范围：只取日期与时分，省去秒与时区，读起来更短。"""
    first, last = args.get(start), args.get(end)
    if not isinstance(first, str) or not isinstance(last, str):
        return None
    return f"{first[:16].replace('T', ' ')} 至 {last[:16].replace('T', ' ')}"


@tool(
    name="calendar_list_events",
    side_effect=SideEffect.READONLY,
    activity_renderer=lambda args: activity("正在查询日程", _span(args, "time_min", "time_max")),
)
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


@tool(
    name="calendar_get_event",
    side_effect=SideEffect.READONLY,
    activity_renderer=lambda args: activity("正在读取日程"),
)
def get_event(event_id: str, *, calendar: CalendarReader) -> dict:
    """按 event_id 读取主日历中的单个事件完整内容。"""
    return calendar.get_event(event_id)


@tool(
    name="calendar_check_conflicts",
    side_effect=SideEffect.READONLY,
    activity_renderer=lambda args: activity("正在检查日程冲突", _span(args, "start", "end")),
)
def check_conflicts(
    start: str, end: str, calendar_id: str = PRIMARY_CALENDAR, *, calendar: CalendarReader
) -> dict:
    """查询目标时间内的忙碌区间与冲突事件；不替用户作结论。"""
    return calendar.check_conflicts(start, end, calendar_id)


@tool(
    name="calendar_create_event",
    side_effect=SideEffect.DIRECT_EXTERNAL_WRITE,
    activity_renderer=lambda args: activity("正在创建日程", args.get("summary")),
)
def create_event(
    summary: str,
    start: str,
    end: str,
    all_day: bool = False,
    location: str | None = None,
    description: str = "",
    calendar_id: str = PRIMARY_CALENDAR,
    overwrite_conflicts: bool = False,
    *,
    task_id: str,
    calendar: CalendarReader,
    calendar_events: CalendarEventStore,
    confirmations: EventCreator,
) -> dict:
    """在用户的 iCloud 主日历中创建单次日程，不邀请也不通知任何人，无需用户再次确认。

    只在用户本轮已经给出标题、开始和结束时间时调用；缺任何一项都先向用户问清楚，不要自行假定。
    目标时间已有日程时不创建、也不留下任何记录，只在 conflicts 中返回撞车的日程；
    此时向用户说明冲突并请其决定改时间还是照建，不要声称已经创建。
    只有用户在本轮明确表示即使冲突也要创建时，才把 overwrite_conflicts 传为真再调用。
    """
    fields = validate_event(
        {
            "summary": summary,
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": location,
            "description": description,
            "calendar_id": calendar_id,
        }
    )
    window_start, window_end = calendar.conflict_window(fields)
    found = calendar.check_conflicts(window_start, window_end, fields["calendar_id"])
    if found["conflicts"] and not overwrite_conflicts:
        return {"status": "conflict", "conflicts": found["conflicts"]}
    saved = calendar_events.save_event(task_id, **fields)
    operation_id, version = saved["operation_id"], saved["version"]
    confirmations.accept_confirmation(task_id, operation_id, version)
    execution = confirmations.execute_accepted(operation_id, deliver=False)
    if execution is None:
        execution = confirmations.get_execution(operation_id)
    result = execution["result"] or {"status": execution["status"]}
    return {"operation_id": operation_id, "version": version, **result}
