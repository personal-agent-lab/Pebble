"""本机产品HTTP驱动；绕过代理，只返回原始响应，不评价场景。"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body


class PebbleClient:
    def __init__(self, base_url, timeout=30):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method, path, *, form=None, json_body=None):
        headers, data = {"Accept": "application/json"}, None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, error.read().decode(errors="replace")) from error
        return json.loads(raw) if raw else None

    def health(self):
        return self._request("GET", "/api/health")

    def create_task(self, *, message, model):
        return self._request("POST", "/api/tasks", form={"message": message, "model": model})

    def send_message(self, task_id, *, message, target=None):
        form = {"message": message}
        if target is not None:
            form["target"] = json.dumps(target)
        return self._request("POST", f"/api/tasks/{task_id}/messages", form=form)

    def get_task(self, task_id):
        return self._request("GET", f"/api/tasks/{task_id}")

    def wait_turn(self, task_id, run_id, *, timeout, poll_interval=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.get_task(task_id).get("latest_run")
            if (
                run
                and run["run_id"] == run_id
                and run["status"] in {"done", "error", "interrupted"}
            ):
                return run
            time.sleep(poll_interval)
        raise TimeoutError("轮次等待超时")

    def timeline(self, task_id):
        return self._request("GET", f"/api/tasks/{task_id}/timeline")["items"]

    def observations(self, task_id):
        return self._request("GET", f"/api/tasks/{task_id}/observations")["runs"]

    def get_draft(self, operation_id):
        return self._request("GET", f"/api/operations/{operation_id}/draft")

    def patch_draft(self, operation_id, *, expected_version, body, to, subject):
        return self._request(
            "PATCH",
            f"/api/operations/{operation_id}/draft",
            json_body={
                "expected_version": expected_version,
                "body": body,
                "to": to,
                "subject": subject,
            },
        )

    def confirm(self, task_id, operation_id, version):
        return self._request(
            "POST",
            f"/api/tasks/{task_id}/confirmations",
            json_body={"operation_id": operation_id, "version": version},
        )

    def cancel(self, task_id, operation_id, version):
        return self._request(
            "POST",
            f"/api/tasks/{task_id}/cancellations",
            json_body={"operation_id": operation_id, "version": version},
        )
