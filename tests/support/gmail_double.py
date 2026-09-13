"""Gmail 的测试替身：校验只拒空正文，发送把参数写进 sent.jsonl，不连接真实邮箱。

发送结果由 `PEBBLE_TEST_SEND_STATUS` 指定（`sent`、`failed`、`unknown`），
`PEBBLE_TEST_SEND_DELAY` 给发送加秒级延时，便于在页面上看到 sending 中间态。
"""

import json
import os
import time
from pathlib import Path

from server.config import get_settings


def sent_log() -> Path:
    return get_settings().data_dir / "sent.jsonl"


def validate(**fields):
    if not fields["body"]:
        return {"valid": False, "errors": [{"field": "body", "message": "正文不能为空"}]}
    return {"valid": True, "errors": []}


def send(**fields):
    with sent_log().open("a") as output:
        output.write(json.dumps(fields, ensure_ascii=False) + "\n")
    time.sleep(float(os.environ.get("PEBBLE_TEST_SEND_DELAY", "0")))
    status = os.environ.get("PEBBLE_TEST_SEND_STATUS", "sent")
    if status == "sent":
        return {"status": status, "message_id": "test-message"}
    return {"status": status, "reason": "测试替身模拟结果：" + status}
