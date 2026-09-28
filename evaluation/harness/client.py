"""驱动器 HTTP 客户端：只走产品对外接口（与前端同款），不碰实例内部状态。

轮次完成采用轮询（`GET /api/tasks/{id}` 的 `latest_run`），与验收脚本同一手法；
SSE 事件流留待需要卡片时延指标的场景（M 层）再接入。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

TERMINAL_STATUSES = frozenset({"done", "error", "interrupted"})


class ApiError(RuntimeError):
    """非 2xx 响应；code 是产品错误码（如 version_conflict），便于场景断言。"""

    def __init__(self, status: int, code: str, message: str, body: dict[str, Any]) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body = body


class Attachment:
    """一条用户附件：文件名 + 内容 + MIME 类型。"""

    def __init__(self, filename: str, data: bytes, mime_type: str) -> None:
        self.filename = filename
        self.data = data
        self.mime_type = mime_type


def _encode_multipart(
    fields: dict[str, str], files: list[tuple[str, Attachment]], boundary: str
) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    for field_name, attachment in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{attachment.filename}"\r\n'
                f"Content-Type: {attachment.mime_type}\r\n\r\n"
            ).encode()
            + attachment.data
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


class PebbleClient:
    """极薄的产品 API 客户端；返回原始 JSON 结构，断言在场景与横切检查里做。"""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---- 基础请求 ----

    def _request(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, str] | None = None,
        files: list[tuple[str, Attachment]] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        data: bytes | None = None
        if files:
            boundary = f"eval-{uuid.uuid4().hex}"
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            data = _encode_multipart(form or {}, files, boundary)
        elif form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(form).encode()
        elif json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            body = self._parse_json(raw)
            code = str(body.get("error", "unknown"))
            message = str(body.get("message", raw.decode("utf-8", "replace")))
            raise ApiError(exc.code, code, message, body) from exc
        if not raw:
            return None
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: bytes) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "non_json", "message": raw.decode("utf-8", "replace")}

    # ---- 健康与模型 ----

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    def models(self) -> dict[str, Any]:
        return self._request("GET", "/api/models")

    # ---- 任务与消息 ----

    def create_task(
        self,
        *,
        message: str,
        model: str,
        files: list[Attachment] | None = None,
        selection: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        form: dict[str, str] = {"model": model, "message": message}
        if task_id:
            form["task_id"] = task_id
        if selection is not None:
            form["selection"] = json.dumps(selection, ensure_ascii=False)
        attachments = [("files", item) for item in (files or [])]
        return self._request("POST", "/api/tasks", form=form, files=attachments or None)

    def send_message(
        self,
        task_id: str,
        *,
        message: str,
        files: list[Attachment] | None = None,
        target: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        form: dict[str, str] = {"message": message}
        if target is not None:
            form["target"] = json.dumps(target, ensure_ascii=False)
        if selection is not None:
            form["selection"] = json.dumps(selection, ensure_ascii=False)
        attachments = [("files", item) for item in (files or [])]
        return self._request(
            "POST", f"/api/tasks/{task_id}/messages", form=form, files=attachments or None
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{task_id}")

    def delete_task(self, task_id: str) -> None:
        self._request("DELETE", f"/api/tasks/{task_id}")

    def interrupt(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/tasks/{task_id}/interrupt")

    # ---- 轮次等待与取证 ----

    def wait_turn(
        self, task_id: str, run_id: str, *, timeout: float, poll_interval: float = 2.0
    ) -> dict[str, Any]:
        """等待指定轮次进入终态；轮内串行调度，latest_run 即当前轮。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.get_task(task_id)
            run = record.get("latest_run")
            if run and run.get("run_id") == run_id and run.get("status") in TERMINAL_STATUSES:
                return run
            time.sleep(poll_interval)
        raise TimeoutError(f"轮次 {run_id} 在 {timeout}s 内未完成")

    def timeline(self, task_id: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/api/tasks/{task_id}/timeline")
        return list(payload.get("items", []))

    def observations(self, task_id: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/api/tasks/{task_id}/observations")
        return list(payload.get("runs", []))

    def wait_observation(
        self, task_id: str, run_id: str, *, timeout: float, poll_interval: float = 2.0
    ) -> dict[str, Any] | None:
        """等待该轮观测定稿（后台标题/记忆任务收尾后 context_after 才写入）。"""
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            for run in self.observations(task_id):
                if run.get("run_id") == run_id:
                    latest = run
                    if run.get("context_after") is not None:
                        return run
            time.sleep(poll_interval)
        return latest

    # ---- 草稿与确认 ----

    def get_draft(self, operation_id: str, version: int | None = None) -> dict[str, Any]:
        path = f"/api/operations/{operation_id}/draft"
        if version is not None:
            path += f"?version={version}"
        return self._request("GET", path)

    def patch_draft(
        self,
        operation_id: str,
        *,
        expected_version: int,
        body: str,
        to: list[str] | None = None,
        subject: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"expected_version": expected_version, "body": body}
        if to is not None:
            payload["to"] = to
        if subject is not None:
            payload["subject"] = subject
        return self._request("PATCH", f"/api/operations/{operation_id}/draft", json_body=payload)

    def confirm(self, task_id: str, operation_id: str, version: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/tasks/{task_id}/confirmations",
            json_body={"operation_id": operation_id, "version": version},
        )

    def cancel(self, task_id: str, operation_id: str, version: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/tasks/{task_id}/cancellations",
            json_body={"operation_id": operation_id, "version": version},
        )

    def execution(self, operation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/operations/{operation_id}/execution")

    # ---- 用户侧管理面（界面编辑等一等公民入口） ----

    def get_memory(self) -> dict[str, Any]:
        return self._request("GET", "/api/memory")

    def put_memory(self, target: str, content: str, expected_version: str) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/api/memory/{target}",
            json_body={"content": content, "expected_version": expected_version},
        )

    def kb_search(self, query: str) -> dict[str, Any]:
        return self._request("GET", f"/api/kb/search?q={urllib.parse.quote(query)}")

    def kb_documents(self) -> dict[str, Any]:
        return self._request("GET", "/api/kb/documents")

    def kb_document(self, path: str) -> dict[str, Any]:
        return self._request("GET", f"/api/kb/document?path={urllib.parse.quote(path)}")

    def kb_update_document(
        self, path: str, expected_version: str, body: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": path, "expected_version": expected_version}
        if body is not None:
            payload["body"] = body
        return self._request("POST", "/api/kb/document/update", json_body=payload)

    def skill_changes(self, status: str | None = None) -> list[dict[str, Any]]:
        path = "/api/skill-changes" + (f"?status={status}" if status else "")
        payload = self._request("GET", path)
        return list(payload if isinstance(payload, list) else payload.get("changes", []))
