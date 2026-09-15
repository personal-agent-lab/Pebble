"""日程字段校验与不可变内容版本。"""

import sqlite3
from datetime import datetime
from pathlib import Path

from server.config import get_settings
from server.db import session, write
from server.errors import DependencyUnavailableError, DraftValidationError, NotFoundError
from server.sessions import repository as operations
from server.sessions.service import create_operation, timestamp

PRIMARY_CALENDAR = "primary"


def validate_event(fields: dict) -> dict:
    result = dict(fields)
    errors: list[dict[str, str]] = []
    summary = fields.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append({"field": "summary", "message": "请提供日程标题"})
    all_day = fields.get("all_day", False)
    if not isinstance(all_day, bool):
        errors.append({"field": "all_day", "message": "全天标记必须是布尔值"})
    parsed: dict[str, datetime] = {}
    for name in ("start", "end"):
        raw = fields.get(name)
        try:
            if not isinstance(raw, str):
                raise ValueError
            if all_day:
                if "T" in raw:
                    raise ValueError
                parsed[name] = datetime.combine(
                    datetime.fromisoformat(raw).date(), datetime.min.time()
                )
            else:
                if "T" not in raw:
                    raise ValueError
                value = datetime.fromisoformat(raw)
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError
                parsed[name] = value
        except (TypeError, ValueError):
            message = (
                "全天日程必须使用 YYYY-MM-DD" if all_day else "请使用带时区偏移的 ISO 8601 时间"
            )
            errors.append({"field": name, "message": message})
    if len(parsed) == 2:
        invalid = parsed["end"] < parsed["start"] if all_day else parsed["end"] <= parsed["start"]
        if invalid:
            message = "结束日期不能早于开始日期" if all_day else "结束时间必须晚于开始时间"
            errors.append({"field": "end", "message": message})
    if fields.get("calendar_id", PRIMARY_CALENDAR) != PRIMARY_CALENDAR:
        errors.append({"field": "calendar_id", "message": "当前只支持主日历 primary"})
    for name in ("location", "description"):
        if fields.get(name) is not None and not isinstance(fields.get(name), str):
            errors.append({"field": name, "message": "必须是字符串或空值"})
    if errors:
        raise DraftValidationError(errors)
    result["summary"] = summary.strip()
    result["all_day"] = all_day
    result["location"] = fields.get("location") or None
    result["description"] = fields.get("description") or ""
    result["calendar_id"] = PRIMARY_CALENDAR
    return result


def stored_event(conn: sqlite3.Connection, operation_id: str, version: int | None = None) -> dict:
    """操作指定版本的日程内容；执行与核实只读这里，不接受调用方另给字段。"""
    operation = operations.operation(conn, operation_id)
    if operation["type"] != "calendar":
        raise NotFoundError(operation_id)
    selected = operation["version"] if version is None else version
    row = conn.execute(
        "SELECT e.calendar_id, e.account, e.calendar_url, v.* FROM calendar_events e "
        "JOIN calendar_event_versions v ON v.operation_id=e.operation_id "
        "WHERE e.operation_id=? AND v.version=?",
        (operation_id, selected),
    ).fetchone()
    if row is None:
        raise NotFoundError(operation_id)
    value = dict(row)
    value.update(operation_id=operation_id, version=selected, status=operation["status"])
    value["all_day"] = bool(value["all_day"])
    return value


class CalendarEventStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def save_event(self, task_id: str, **fields) -> dict:
        """为一次创建保存不可变内容版本；每次调用都新建操作，不复用历史操作。"""
        fields = validate_event(fields)
        settings = get_settings()
        if (
            not settings.icloud_account
            or not settings.icloud_password_path
            or not settings.icloud_calendar_url
        ):
            raise DependencyUnavailableError("请先配置 iCloud 账号、App 专用密码文件和主日历地址")
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            operation_id = create_operation(conn, task_id, "calendar")["operation_id"]
            conn.execute(
                "INSERT INTO calendar_events "
                "(operation_id,calendar_id,account,calendar_url) VALUES (?,?,?,?)",
                (
                    operation_id,
                    PRIMARY_CALENDAR,
                    settings.icloud_account,
                    settings.icloud_calendar_url,
                ),
            )
            conn.execute(
                "INSERT INTO calendar_event_versions "
                "(operation_id,version,summary,start,end,all_day,location,description,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    1,
                    fields["summary"],
                    fields["start"],
                    fields["end"],
                    int(fields["all_day"]),
                    fields["location"],
                    fields["description"],
                    timestamp(),
                ),
            )
            return {"operation_id": operation_id, "version": 1}
