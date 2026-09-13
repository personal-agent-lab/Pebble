"""日历网页验收替身：真实 HTTP/SQLite/确认；模型与外部创建为替身。"""

import json
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.config import get_settings
from server.main import create_app
from server.tools.calendar import tools
from server.tools.calendar.client import resource_url

FIELDS = dict(
    title="项目会议",
    start="2026-10-12T10:00:00+10:30",
    end="2026-10-12T11:00:00+10:30",
    timezone="Australia/Adelaide",
    location="会议室",
    description="中文备注\n不发送邀请",
)


class CalendarAgent:
    async def stream_message(self, *, task_id, sdk_session_id, message):
        yield {"type": "session", "sdk_session_id": sdk_session_id or task_id}
        saved = tools.prepare_event(task_id, "demo-event", **FIELDS)
        yield {
            "type": "draft_saved",
            "operation_id": saved["operation_id"],
            "version": saved["version"],
        }
        yield {"type": "text", "text": "日程预览已准备好，请审阅确认；未检查日历冲突。"}
        yield {"type": "done"}

    async def stream_execution_result(self, **values):
        yield {"type": "text", "text": "日程创建结果：" + values["result"]["status"]}
        yield {"type": "done"}

    async def read_history(self, **values):
        return []


def create_event(**values):
    with (get_settings().data_dir / "calendar_writes.jsonl").open("a") as handle:
        handle.write(json.dumps(values, ensure_ascii=False) + "\n")
    return {
        "status": "created",
        "uid": values["draft"]["uid"],
        "resource_url": resource_url(values["draft"]),
    }


app = create_app(gateway=CalendarAgent(), create_event=create_event)
dist = Path(__file__).resolve().parents[2] / "web" / "dist"
app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    return FileResponse(dist / "index.html")
