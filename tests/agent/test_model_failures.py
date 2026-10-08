import pytest
from qodercn_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from server.agent.failures import assistant_failure, result_failure


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("authentication_failed", "model_authentication"),
        ("billing_error", "model_billing"),
        ("rate_limit", "model_rate_limited"),
        ("invalid_request", "model_invalid_request"),
        ("server_error", "model_unavailable"),
    ],
)
def test_sdk_explicit_errors_are_classified_without_exposing_response(error, code):
    message = AssistantMessage([TextBlock("credential=private-key")], "<synthetic>", error=error)
    failure = assistant_failure(message)
    assert failure.code == code
    assert "private-key" not in str(failure.payload())


def test_refusal_alone_does_not_claim_content_filtering():
    result = ResultMessage(
        "error_during_execution", 1, 1, True, 1, "session", stop_reason="refusal"
    )
    assert result_failure(result).code == "model_refused"
    normal = AssistantMessage([TextBlock("This conversation contains sensitive content.")], "model")
    assert assistant_failure(normal) is None


def test_arbitrary_result_text_is_not_a_user_facing_error():
    result = ResultMessage("error", 1, 1, True, 1, "s", result="Bearer secret-token")
    failure = result_failure(result)
    assert failure.code == "model_failed"
    assert "secret-token" not in str(failure.payload())
