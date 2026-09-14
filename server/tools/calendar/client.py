"""iCloud CalDAV 查询、条件创建与只读核实。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from caldav import Calendar as DAVCalendar
from caldav import DAVClient
from caldav.lib.error import AuthorizationError
from caldav.lib.error import NotFoundError as DAVNotFoundError
from icalendar import Calendar, Event

from server.config import Settings
from server.errors import DependencyUnavailableError, DraftValidationError, NotFoundError
from server.tools.calendar.service import PRIMARY_CALENDAR


def _range(start: str, end: str) -> tuple[datetime, datetime]:
    errors = []
    parsed = []
    for name, raw in (("start", start), ("end", end)):
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
            parsed.append(value)
        except (TypeError, ValueError):
            errors.append({"field": name, "message": "请使用带时区偏移的 ISO 8601 时间"})
    if not errors and parsed[1] <= parsed[0]:
        errors.append({"field": "end", "message": "结束时间必须晚于开始时间"})
    if errors:
        raise DraftValidationError(errors)
    return parsed[0], parsed[1]


def _iso(value: date | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return value.isoformat()


def _component(resource) -> Event:
    components = Calendar.from_ical(resource.data).walk("VEVENT")
    if not components:
        raise ValueError("日历资源不包含事件")
    return components[0]


def _event_times(component: Event) -> tuple[date | datetime, date | datetime]:
    start = component.decoded("DTSTART")
    if component.get("DTEND") is not None:
        return start, component.decoded("DTEND")
    if component.get("DURATION") is not None:
        return start, start + component.decoded("DURATION")
    raise ValueError("日历事件缺少结束时间")


def _moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _attendees(component: Event) -> list[dict]:
    raw = component.get("ATTENDEE", [])
    values = raw if isinstance(raw, list) else [raw]
    return [
        {
            "email": str(value).removeprefix("mailto:").removeprefix("MAILTO:"),
            "response_status": str(value.params.get("PARTSTAT", "NEEDS-ACTION")).lower(),
        }
        for value in values
    ]


def event_record(resource, *, description: bool = False) -> dict:
    component = _component(resource)
    start, end = _event_times(component)
    modified = component.get("LAST-MODIFIED") or component.get("DTSTAMP")
    if modified is None:
        raise ValueError("日历事件缺少更新时间")
    recurrence = []
    for name in ("RRULE", "RDATE", "EXDATE"):
        value = component.get(name)
        if value is not None:
            encoded = value.to_ical().decode("utf-8") if hasattr(value, "to_ical") else str(value)
            recurrence.append(f"{name}:{encoded}")
    record = {
        "event_id": str(component.get("UID", "")),
        "summary": str(component.get("SUMMARY", "")),
        "location": str(component.get("LOCATION", "")) or None,
        "start": _iso(start),
        "end": _iso(end),
        "all_day": isinstance(start, date) and not isinstance(start, datetime),
        "recurrence": recurrence or None,
        "status": str(component.get("STATUS", "CONFIRMED")).lower(),
        "attendees": _attendees(component),
        "updated_at": _iso(modified.dt),
    }
    if description:
        record["description"] = str(component.get("DESCRIPTION", ""))
    return record


class CalDAVCalendarClient:
    def __init__(self, settings: Settings):
        self.account = settings.icloud_account
        self.password_path = settings.icloud_password_path
        self.calendar_url = settings.icloud_calendar_url
        if not self.account or not self.password_path or not self.calendar_url:
            raise DependencyUnavailableError("请先配置 iCloud 账号、App 专用密码文件和主日历地址")
        url = urlsplit(self.calendar_url)
        if url.scheme != "https" or not (url.hostname or "").endswith(".icloud.com"):
            raise DependencyUnavailableError("iCloud 主日历地址无效")
        if url.username or url.password or url.query or url.fragment:
            raise DependencyUnavailableError("iCloud 主日历地址不能包含凭证、查询或片段")

    def _connection(self) -> DAVClient:
        try:
            password = self.password_path.read_text().strip()
        except OSError as error:
            raise DependencyUnavailableError("无法读取 iCloud App 专用密码文件") from error
        if not password:
            raise DependencyUnavailableError("iCloud App 专用密码文件为空")
        client = DAVClient(
            url=self.calendar_url,
            username=self.account,
            password=password,
            auth_type="basic",
            timeout=30,
            enable_rfc6764=False,
            rate_limit_handle=False,
        )
        client.session.max_redirects = 0
        return client

    def _calendar(self, connection: DAVClient) -> DAVCalendar:
        return DAVCalendar(client=connection, url=self.calendar_url)

    @staticmethod
    def _calendar_id(calendar_id: str) -> None:
        if calendar_id != PRIMARY_CALENDAR:
            raise DraftValidationError(
                [{"field": "calendar_id", "message": "当前只支持主日历 primary"}]
            )

    def list_events(
        self,
        time_min: str,
        time_max: str,
        calendar_id: str = PRIMARY_CALENDAR,
        max_results: int = 50,
    ) -> dict:
        self._calendar_id(calendar_id)
        start, end = _range(time_min, time_max)
        if max_results < 1 or max_results > 200:
            raise DraftValidationError(
                [{"field": "max_results", "message": "必须在 1 到 200 之间"}]
            )
        try:
            with self._connection() as connection:
                resources = self._calendar(connection).search(
                    start=start, end=end, event=True, expand=False
                )
                events = [event_record(resource) for resource in resources[:max_results]]
            return {"events": events}
        except (DependencyUnavailableError, DraftValidationError):
            raise
        except Exception as error:
            raise DependencyUnavailableError("无法读取 iCloud 日历") from error

    def get_event(self, event_id: str) -> dict:
        if not event_id:
            raise DraftValidationError([{"field": "event_id", "message": "不能为空"}])
        try:
            with self._connection() as connection:
                resource = self._calendar(connection).event_by_uid(event_id)
                return event_record(resource, description=True)
        except DAVNotFoundError as error:
            raise NotFoundError(event_id) from error
        except Exception as error:
            raise DependencyUnavailableError("无法读取 iCloud 日程") from error

    def check_conflicts(self, start: str, end: str, calendar_id: str = PRIMARY_CALENDAR) -> dict:
        self._calendar_id(calendar_id)
        time_min, time_max = _range(start, end)
        try:
            with self._connection() as connection:
                resources = self._calendar(connection).search(
                    start=time_min, end=time_max, event=True, expand=True, split_expanded=True
                )
                conflicts = [event_record(resource) for resource in resources]
        except Exception as error:
            raise DependencyUnavailableError("无法检查 iCloud 日历冲突") from error
        conflicts = [event for event in conflicts if event["status"] != "cancelled"]
        intervals = sorted((event["start"], event["end"]) for event in conflicts)
        busy: list[dict[str, str]] = []
        for interval_start, interval_end in intervals:
            if not busy or _moment(interval_start) > _moment(busy[-1]["end"]):
                busy.append({"start": interval_start, "end": interval_end})
            elif _moment(interval_end) > _moment(busy[-1]["end"]):
                busy[-1]["end"] = interval_end
        return {"busy": busy, "conflicts": conflicts}

    @staticmethod
    def uid(operation_id: str) -> str:
        return f"pebble-{operation_id}@local"

    def _resource_url(self, operation_id: str) -> str:
        return self.calendar_url.rstrip("/") + "/" + self.uid(operation_id) + ".ics"

    def _event_data(self, operation_id: str, fields: dict) -> str:
        calendar = Calendar()
        calendar.add("prodid", "-//Pebble//Calendar//EN")
        calendar.add("version", "2.0")
        event = Event()
        event.add("uid", self.uid(operation_id))
        event.add("dtstamp", datetime.now(UTC))
        event.add("summary", fields["summary"])
        if fields["all_day"]:
            event.add("dtstart", date.fromisoformat(fields["start"]))
            event.add("dtend", date.fromisoformat(fields["end"]))
        else:
            event.add("dtstart", datetime.fromisoformat(fields["start"]).astimezone(UTC))
            event.add("dtend", datetime.fromisoformat(fields["end"]).astimezone(UTC))
        if fields.get("location"):
            event.add("location", fields["location"])
        if fields.get("description"):
            event.add("description", fields["description"])
        calendar.add_component(event)
        return calendar.to_ical().decode("utf-8")

    def _matches(self, data: str, operation_id: str, fields: dict) -> bool:
        try:
            components = Calendar.from_ical(data).walk("VEVENT")
            if len(components) != 1:
                return False
            event = components[0]
            if any(name in event for name in ("RRULE", "RDATE", "ATTENDEE", "ORGANIZER")):
                return False
            actual = {
                "summary": str(event.get("SUMMARY", "")),
                "start": _iso(event.decoded("DTSTART")),
                "end": _iso(event.decoded("DTEND")),
                "all_day": isinstance(event.decoded("DTSTART"), date)
                and not isinstance(event.decoded("DTSTART"), datetime),
                "location": str(event.get("LOCATION", "")) or None,
                "description": str(event.get("DESCRIPTION", "")),
            }
            expected = {
                key: fields[key]
                for key in ("summary", "start", "end", "all_day", "location", "description")
            }
            if not fields["all_day"]:
                actual["start"] = (
                    datetime.fromisoformat(actual["start"]).astimezone(UTC).isoformat()
                )
                actual["end"] = datetime.fromisoformat(actual["end"]).astimezone(UTC).isoformat()
                expected["start"] = (
                    datetime.fromisoformat(expected["start"]).astimezone(UTC).isoformat()
                )
                expected["end"] = (
                    datetime.fromisoformat(expected["end"]).astimezone(UTC).isoformat()
                )
            return str(event.get("UID", "")) == self.uid(operation_id) and actual == expected
        except Exception:
            return False

    @staticmethod
    def _unknown() -> dict:
        return {"status": "unknown", "reason": "日程创建结果待核实，不会自动再次创建"}

    def create_event(self, *, operation_id: str, version: int, fields: dict) -> dict:
        if fields.get("account") != self.account or fields.get("calendar_url") != self.calendar_url:
            return {"status": "failed", "reason": "iCloud 日历配置已变化，请重新准备预览"}
        if fields["all_day"]:
            conflict_start = datetime.combine(
                date.fromisoformat(fields["start"]), datetime.min.time(), UTC
            ).isoformat()
            conflict_end = datetime.combine(
                date.fromisoformat(fields["end"]), datetime.min.time(), UTC
            ).isoformat()
        else:
            conflict_start, conflict_end = fields["start"], fields["end"]
        conflicts = self.check_conflicts(conflict_start, conflict_end, fields["calendar_id"])
        if conflicts["conflicts"]:
            return {"status": "failed", "reason": "目标时间已有日程冲突，请重新选择时间"}
        try:
            with self._connection() as connection:
                response = connection.put(
                    self._resource_url(operation_id),
                    self._event_data(operation_id, fields),
                    headers={"If-None-Match": "*", "Content-Type": "text/calendar; charset=utf-8"},
                )
                if response.status not in (201, 204, 412):
                    if response.status in (400, 403, 404, 405, 409, 415, 422):
                        return {"status": "failed", "reason": "iCloud 拒绝创建，请检查日历写入权限"}
                    return self._unknown()
                return self._verify_with(connection, operation_id, fields)
        except AuthorizationError:
            return {"status": "failed", "reason": "iCloud 认证或写入权限失败"}
        except Exception:
            return self._unknown()

    def _verify_with(self, connection: DAVClient, operation_id: str, fields: dict) -> dict:
        try:
            response = connection.request(self._resource_url(operation_id))
        except Exception:
            return self._unknown()
        if response.status == 200 and self._matches(response.raw, operation_id, fields):
            return {"status": "created", "event_id": self.uid(operation_id)}
        return self._unknown()

    def verify_event(self, *, operation_id: str, fields: dict) -> dict:
        try:
            with self._connection() as connection:
                return self._verify_with(connection, operation_id, fields)
        except Exception:
            return self._unknown()
