"""日程预览校验、不可变版本与任务关联。"""

import sqlite3
from datetime import datetime
from pathlib import Path

from server.config import get_settings
from server.db import session, write
from server.errors import DependencyUnavailableError, DraftValidationError, NotFoundError
from server.sessions import repository as operations
from server.sessions.service import create_operation, next_version, timestamp

PRIMARY_CALENDAR = "primary"


def validate_preview(fields: dict) -> dict:
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


def preview(conn: sqlite3.Connection, operation_id: str, version: int | None = None) -> dict:
    operation = operations.operation(conn, operation_id)
    if operation["type"] != "calendar":
        raise NotFoundError(operation_id)
    selected = operation["version"] if version is None else version
    row = conn.execute(
        "SELECT p.calendar_id, p.account, p.calendar_url, v.* FROM calendar_previews p "
        "JOIN calendar_preview_versions v ON v.operation_id=p.operation_id "
        "WHERE p.operation_id=? AND v.version=?",
        (operation_id, selected),
    ).fetchone()
    if row is None:
        raise NotFoundError(operation_id)
    value = dict(row)
    value.update(operation_id=operation_id, version=selected, status=operation["status"])
    value["all_day"] = bool(value["all_day"])
    return value


def _normalized_key(fields: dict) -> tuple:
    return (
        fields["summary"].casefold(),
        fields["start"],
        fields["end"],
        int(fields["all_day"]),
    )


class CalendarPreviewStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def get_preview(self, operation_id: str, version: int | None = None) -> dict:
        with session(self.path) as conn:
            value = preview(conn, operation_id, version)
        return {
            key: value for key, value in value.items() if key not in {"account", "calendar_url"}
        }

    def require_task(self, task_id: str, operation_id: str) -> None:
        with session(self.path) as conn:
            if not conn.execute(
                "SELECT 1 FROM task_operations WHERE task_id=? AND operation_id=?",
                (task_id, operation_id),
            ).fetchone():
                raise NotFoundError(operation_id)

    def save_preview(self, task_id: str, **fields) -> dict:
        fields = validate_preview(fields)
        settings = get_settings()
        if (
            not settings.icloud_account
            or not settings.icloud_password_path
            or not settings.icloud_calendar_url
        ):
            raise DependencyUnavailableError("请先配置 iCloud 账号、App 专用密码文件和主日历地址")
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            rows = conn.execute(
                "SELECT o.operation_id FROM operations o "
                "JOIN task_operations t ON t.operation_id=o.operation_id "
                "WHERE t.task_id=? AND o.type='calendar' AND o.status='pending'",
                (task_id,),
            ).fetchall()
            wanted = _normalized_key(fields)
            for row in rows:
                current = preview(conn, row["operation_id"])
                if _normalized_key(current) == wanted:
                    return {
                        "operation_id": row["operation_id"],
                        "version": current["version"],
                        "status": "pending",
                    }
            operation = create_operation(conn, task_id, "calendar")
            operation_id = operation["operation_id"]
            conn.execute(
                "INSERT INTO calendar_previews "
                "(operation_id,calendar_id,account,calendar_url) VALUES (?,?,?,?)",
                (
                    operation_id,
                    PRIMARY_CALENDAR,
                    settings.icloud_account,
                    settings.icloud_calendar_url,
                ),
            )
            self._insert_version(conn, operation_id, 1, fields)
            return {"operation_id": operation_id, "version": 1, "status": "pending"}

    def update_preview(self, operation_id: str, expected_version: int, **fields) -> dict:
        fields = validate_preview(fields)
        with session(self.path) as conn, write(conn):
            preview(conn, operation_id)
            version = next_version(conn, operation_id, expected_version)
            self._insert_version(conn, operation_id, version, fields)
            return {"operation_id": operation_id, "version": version, "status": "pending"}

    @staticmethod
    def _insert_version(
        conn: sqlite3.Connection, operation_id: str, version: int, fields: dict
    ) -> None:
        conn.execute(
            "INSERT INTO calendar_preview_versions "
            "(operation_id,version,summary,start,end,all_day,location,description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                version,
                fields["summary"],
                fields["start"],
                fields["end"],
                int(fields["all_day"]),
                fields["location"],
                fields["description"],
                timestamp(),
            ),
        )
