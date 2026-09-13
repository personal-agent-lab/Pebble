"""日程预览与版本存储；不访问外部服务。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from server.config import get_settings
from server.db import session, write
from server.errors import DependencyUnavailableError, NotFoundError
from server.sessions import repository as operations
from server.sessions.service import create_operation, next_version
from server.tools.gmail.service import DraftValidationError


def validate(fields: dict) -> dict:
    result = dict(fields)
    errors = []
    if not fields.get("title", "").strip():
        errors.append({"field": "title", "message": "请提供日程标题"})
    try:
        zone = ZoneInfo(fields["timezone"])
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        raise DraftValidationError(
            [{"field": "timezone", "message": "请输入有效 IANA 时区"}]
        ) from None
    parsed = {}
    for key in ("start", "end"):
        try:
            raw = fields[key]
            if "T" not in raw:
                raise ValueError
            value = datetime.fromisoformat(raw)
            if value.microsecond:
                raise ValueError
            if value.tzinfo is None:
                first = value.replace(tzinfo=zone, fold=0)
                second = value.replace(tzinfo=zone, fold=1)
                if first.utcoffset() != second.utcoffset():
                    raise ValueError
                value = first
            local = value.astimezone(zone)
            if local.replace(tzinfo=None) != value.replace(tzinfo=None):
                raise ValueError
            if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local.replace(
                tzinfo=None
            ):
                raise ValueError
            parsed[key] = local.astimezone(UTC)
            result[key] = local.isoformat()
        except (KeyError, ValueError, TypeError):
            errors.append(
                {
                    "field": key,
                    "message": "请提供明确的日期时间；偏移须与时区一致，夏令时歧义须明确偏移",
                }
            )
    if len(parsed) == 2 and parsed["end"] <= parsed["start"]:
        errors.append({"field": "end", "message": "结束时间必须晚于开始时间"})
    if errors:
        raise DraftValidationError(errors)
    return result


def draft(conn, operation_id: str, version: int | None = None) -> dict:
    op = operations.operation(conn, operation_id)
    if op["type"] != "calendar_create":
        raise NotFoundError(operation_id)
    row = conn.execute(
        "SELECT d.*, v.fields FROM calendar_drafts d JOIN calendar_versions v "
        "ON v.operation_id=d.operation_id WHERE d.operation_id=? AND v.version=?",
        (operation_id, version if version is not None else op["version"]),
    ).fetchone()
    if row is None:
        raise NotFoundError(operation_id)
    return {
        **op,
        **dict(row),
        **json.loads(row["fields"]),
        "version": version if version is not None else op["version"],
    }


class CalendarDraftStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def get(self, operation_id: str, version: int | None = None) -> dict:
        with session(self.path) as conn:
            value = draft(conn, operation_id, version)
        # Account binding is internal; never expose login information to the model or browser.
        return {k: v for k, v in value.items() if k not in {"account", "fields", "request_id"}}

    def require_task(self, task_id: str, operation_id: str) -> None:
        with session(self.path) as conn:
            if not conn.execute(
                "SELECT 1 FROM task_operations WHERE task_id=? AND operation_id=?",
                (task_id, operation_id),
            ).fetchone():
                raise NotFoundError(operation_id)

    def prepare(self, task_id: str, request_id: str, **fields) -> dict:
        settings = get_settings()
        if (
            not settings.icloud_account
            or not settings.icloud_calendar_url
            or not settings.icloud_password_path
        ):
            raise DependencyUnavailableError("请先配置 iCloud 账号、密码文件及目标日历")
        if not request_id.strip():
            raise DraftValidationError([{"field": "request_id", "message": "准备请求标识不能为空"}])
        fields["timezone"] = fields.get("timezone") or settings.icloud_timezone
        fields = validate(fields)
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            existing = conn.execute(
                "SELECT operation_id FROM calendar_drafts WHERE task_id=? AND request_id=?",
                (task_id, request_id),
            ).fetchone()
            if existing:
                operation_id = existing["operation_id"]
            else:
                op = create_operation(conn, task_id, "calendar_create")
                operation_id = op["operation_id"]
                conn.execute(
                    "INSERT INTO calendar_drafts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        task_id,
                        request_id,
                        str(uuid4()),
                        settings.icloud_account,
                        settings.icloud_calendar_url,
                    ),
                )
                conn.execute(
                    "INSERT INTO calendar_versions VALUES (?, 1, ?)",
                    (operation_id, json.dumps(fields)),
                )
        return self.get(operation_id)

    def update(self, operation_id: str, expected_version: int, **fields) -> dict:
        fields = validate(fields)
        with session(self.path) as conn, write(conn):
            draft(conn, operation_id)
            version = next_version(conn, operation_id, expected_version)
            conn.execute(
                "INSERT INTO calendar_versions VALUES (?, ?, ?)",
                (operation_id, version, json.dumps(fields)),
            )
        return self.get(operation_id)
