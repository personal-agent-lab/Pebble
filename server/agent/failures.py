"""Qoder SDK 错误适配；仅解释明确协议字段和已知服务错误提示。"""

from dataclasses import replace

from qodercn_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from qodercn_agent_sdk._errors import (
    AuthNotConfiguredError,
    CLIConnectionError,
    CLIJSONDecodeError,
    MessageParseError,
    ModelPolicyTimeoutError,
    ProcessError,
)

from server.failures import Failure, FailureError, exception_failure

MODEL_ERRORS = {
    "authentication_failed": ("model_authentication", "模型服务认证失败", "check_configuration"),
    "billing_error": ("model_billing", "模型服务额度或计费异常", "check_configuration"),
    "rate_limit": ("model_rate_limited", "模型服务请求受到限流", "wait"),
    "invalid_request": ("model_invalid_request", "模型服务拒绝了请求", "none"),
    "server_error": ("model_unavailable", "模型服务暂时不可用", "wait"),
}


def assistant_failure(message: AssistantMessage, *, stage: str = "response") -> Failure | None:
    if not message.error:
        return None
    text = "\n".join(block.text for block in message.content if isinstance(block, TextBlock))
    # 当前 SDK 丢弃 isContentFilteredMessage；只在合成错误消息中匹配已知提示。
    # 不能根据正常回答中的政治词汇、HTTP 406 或 refusal 本身推断内容过滤。
    if message.model == "<synthetic>" and (
        "This conversation contains sensitive content." in text
        or "Session blocked, Please clear context try again" in text
    ):
        return Failure(
            "model_content_filtered",
            "模型服务因内容过滤停止了回答",
            "model",
            stage,
            "turn",
            "new_session",
            details={"provider": "Qoder", "session_blocked": True},
        )
    code, label, recovery = MODEL_ERRORS.get(
        message.error, ("model_failed", "模型调用失败", "none")
    )
    request_id = getattr(message, "request_id", None)
    return Failure(
        code,
        label,
        "model",
        stage,
        "turn",
        recovery,
        details={"request_id": request_id} if request_id else {},
    )


def result_failure(
    message: ResultMessage, observed: Failure | None = None, *, stage: str = "response"
) -> Failure:
    details = {"provider": "Qoder", "subtype": message.subtype}
    if message.stop_reason is not None:
        details["stop_reason"] = message.stop_reason
    if isinstance(message.usage, dict) and isinstance(message.usage.get("request_id"), str):
        details["request_id"] = message.usage["request_id"]
    if observed is not None:
        return replace(observed, details={**observed.details, **details})
    if message.stop_reason == "refusal":
        return Failure(
            "model_refused", "模型服务拒绝继续回答", "model", stage, "turn", details=details
        )
    return Failure(
        "model_failed",
        "模型调用失败",
        "model",
        stage,
        "turn",
        details=details,
    )


def sdk_exception(error: Exception, *, stage: str = "sdk") -> Failure:
    if isinstance(error, FailureError):
        return error.failure
    if isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
        child = error.exceptions[0]
        if isinstance(child, Exception):
            return sdk_exception(child, stage=stage)
    if isinstance(error, AuthNotConfiguredError):
        return Failure(
            "model_authentication",
            "模型服务认证配置缺失",
            "sdk",
            stage,
            "turn",
            "check_configuration",
        )
    if isinstance(error, (CLIJSONDecodeError, MessageParseError)):
        return Failure("sdk_protocol", "模型运行时返回了无效数据", "sdk", stage, "turn")
    if isinstance(error, ModelPolicyTimeoutError):
        return Failure("timeout", "模型选择超时", "sdk", stage, "turn")
    if isinstance(error, CLIConnectionError):
        return Failure("sdk_connection", "无法连接模型运行时", "sdk", stage, "turn")
    if isinstance(error, ProcessError):
        return Failure("sdk_process", "模型运行时异常退出", "sdk", stage, "turn")
    return exception_failure(error, source="sdk", stage=stage, impact="turn")
