"""CalDAV 条件创建和只读核实；从不重试 PUT 或覆盖已有事件。"""

from datetime import UTC, datetime
from urllib.parse import urlsplit

from caldav import DAVClient
from caldav.lib.error import AuthorizationError
from icalendar import Calendar, Event

from server.config import get_settings


def resource_url(draft: dict) -> str:
    return draft["calendar_url"].rstrip("/") + "/" + draft["uid"] + ".ics"


def event_data(draft: dict) -> str:
    calendar = Calendar()
    calendar.add("prodid", "-//Pebble//Calendar//EN")
    calendar.add("version", "2.0")
    event = Event()
    event.add("uid", draft["uid"])
    event.add("dtstamp", datetime.fromisoformat(draft["created_at"]))
    event.add("summary", draft["title"])
    event.add("dtstart", datetime.fromisoformat(draft["start"]).astimezone(UTC))
    event.add("dtend", datetime.fromisoformat(draft["end"]).astimezone(UTC))
    event.add("location", draft["location"])
    event.add("description", draft["description"])
    event.add("x-pebble-timezone", draft["timezone"])
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


def matches(data: str, draft: dict) -> bool:
    try:
        calendar = Calendar.from_ical(data)
        events = calendar.walk("VEVENT")
        if len(events) != 1 or "METHOD" in calendar:
            return False
        event = events[0]
        if any(
            key in event for key in ("RRULE", "RDATE", "RECURRENCE-ID", "ATTENDEE", "ORGANIZER")
        ):
            return False
        return (
            str(event.get("uid", "")) == draft["uid"]
            and str(event.get("summary", "")) == draft["title"]
            and str(event.get("location", "")) == draft["location"]
            and str(event.get("description", "")) == draft["description"]
            and str(event.get("x-pebble-timezone", "")) == draft["timezone"]
            and event.decoded("dtstart") == datetime.fromisoformat(draft["start"])
            and event.decoded("dtend") == datetime.fromisoformat(draft["end"])
            and not event.walk("VALARM")
        )
    except Exception:
        return False


def connection(draft: dict):
    settings = get_settings()
    url = urlsplit(draft["calendar_url"])
    if (
        not settings.icloud_password_path
        or draft["account"] != settings.icloud_account
        or draft["calendar_url"] != settings.icloud_calendar_url
        or url.scheme != "https"
        or not (url.hostname or "").endswith(".icloud.com")
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("日历配置无效或已变化，请重新准备预览")
    password = settings.icloud_password_path.read_text().strip()
    if not password:
        raise ValueError("密码文件为空")
    client = DAVClient(
        url=draft["calendar_url"],
        username=draft["account"],
        password=password,
        auth_type="basic",
        timeout=30,
        enable_rfc6764=False,
        rate_limit_handle=False,
    )
    client.session.max_redirects = 0
    return client


def unknown() -> dict:
    return {"status": "unknown", "reason": "日程创建结果待核实，不会自动再次创建"}


def verify_with_client(client, draft: dict) -> dict:
    try:
        response = client.request(resource_url(draft))
    except Exception:
        return unknown()
    if response.status == 200 and matches(response.raw, draft):
        return {"status": "created", "uid": draft["uid"], "resource_url": resource_url(draft)}
    return unknown()


def create_event(*, operation_id: str, version: int, draft: dict) -> dict:
    try:
        client = connection(draft)
    except Exception:
        return {"status": "failed", "reason": "iCloud 服务端配置不可用或已变化，未发起创建"}
    with client:
        try:
            response = client.put(
                resource_url(draft),
                event_data(draft),
                headers={"If-None-Match": "*", "Content-Type": "text/calendar; charset=utf-8"},
            )
            if response.status in (201, 204, 412):
                return verify_with_client(client, draft)
            if response.status in (400, 403, 404, 405, 409, 415, 422):
                return {"status": "failed", "reason": "iCloud 拒绝创建，请检查目标日历和写入权限"}
            return unknown()
        except AuthorizationError:
            return {
                "status": "failed",
                "reason": "iCloud 认证或权限失败，请检查 App 专用密码及目标日历",
            }
        except Exception:
            # Never include provider exceptions, response bodies or credentials in tool results.
            return unknown()


def verify_event(*, draft: dict) -> dict:
    try:
        with connection(draft) as client:
            return verify_with_client(client, draft)
    except Exception:
        return unknown()
