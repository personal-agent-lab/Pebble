"""Gmail 回复草稿业务校验纯函数。

遵循 docs/v1-mail-flow-contract.md §6 约定：
- 供模型首次生成草稿与用户前端后续编辑共用；
- 纯函数逻辑：不保存、不发送、不产生副作用、不自动篡改内容；
- 统一返回: {"valid": bool, "errors": [{"field": str, "message": str}]}。
"""

from __future__ import annotations

import re
from email.utils import parseaddr
from typing import TypedDict

# 匹配标准 Email 格式（支持形如 user.name+tag@domain.co.uk）
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


class ValidationError(TypedDict):
    field: str
    message: str


class ValidationResult(TypedDict):
    valid: bool
    errors: list[ValidationError]


def is_valid_email_address(addr: str) -> bool:
    """检查邮箱地址格式是否合规（支持纯地址或带姓名的 RFC 格式）。"""
    if not addr or not isinstance(addr, str):
        return False

    clean_str = addr.strip()
    if not clean_str:
        return False

    # 若包含 Name <email> 结构，提取实际 email 地址
    _, parsed_email = parseaddr(clean_str)
    target = parsed_email.strip() if parsed_email else clean_str
    return bool(EMAIL_REGEX.match(target))


def validate_reply_draft(
    source_message_id: str,
    thread_id: str,
    to: list[str] | str,
    subject: str,
    body: str,
) -> ValidationResult:
    """回复草稿业务规则校验纯函数。

    Args:
        source_message_id: 原邮件标识（供定位与去重）
        thread_id: Gmail 邮件线程 ID
        to: 收件人地址列表（或逗号分隔字符串）
        subject: 回复主题
        body: 回复正文

    Returns:
        ValidationResult 字典结构: {"valid": bool, "errors": [...]}
    """
    errors: list[ValidationError] = []

    # 1. 校验 source_message_id 和 thread_id 必须存在且合法
    if not isinstance(source_message_id, str) or not source_message_id.strip():
        errors.append(
            {
                "field": "source_message_id",
                "message": "原邮件标识 (source_message_id) 必须存在且不能为空",
            }
        )
    elif re.search(r"\s", source_message_id):
        errors.append(
            {
                "field": "source_message_id",
                "message": "原邮件标识 (source_message_id) 不合法，不能包含空白字符",
            }
        )

    if not isinstance(thread_id, str) or not thread_id.strip():
        errors.append(
            {
                "field": "thread_id",
                "message": "邮件线程标识 (thread_id) 必须存在且不能为空",
            }
        )
    elif re.search(r"\s", thread_id):
        errors.append(
            {
                "field": "thread_id",
                "message": "邮件线程标识 (thread_id) 不合法，不能包含空白字符",
            }
        )

    # 2. 校验 to 必须为非空且符合 RFC 5322 格式的有效邮箱地址列表
    recipient_list: list[str] = []
    if isinstance(to, list):
        recipient_list = [str(addr).strip() for addr in to if str(addr).strip()]
    elif isinstance(to, str):
        recipient_list = [addr.strip() for addr in to.split(",") if addr.strip()]

    if not recipient_list:
        errors.append(
            {
                "field": "to",
                "message": "收件人 (to) 必须为非空且符合 RFC 5322 格式的有效邮箱地址列表",
            }
        )
    else:
        for addr in recipient_list:
            if not is_valid_email_address(addr):
                errors.append(
                    {
                        "field": "to",
                        "message": f"收件人地址不符合 RFC 5322 格式规范: {addr}",
                    }
                )

    # 3. 校验 subject 与 body 不能为空或全空白字符
    if not isinstance(subject, str) or not subject.strip():
        errors.append(
            {
                "field": "subject",
                "message": "邮件主题 (subject) 不能为空或全空白字符",
            }
        )

    if not isinstance(body, str) or not body.strip():
        errors.append(
            {
                "field": "body",
                "message": "邮件正文 (body) 不能为空或全空白字符",
            }
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }
