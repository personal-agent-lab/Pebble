import logging
import sqlite3

from server.failures import exception_failure, log_failure


def test_unknown_failure_never_exports_exception_values(caplog):
    error = RuntimeError("Authorization: Bearer private-token; password=private-password")
    failure = exception_failure(error, source="gateway", stage="stream", impact="turn")
    with caplog.at_level(logging.ERROR):
        log_failure(logging.getLogger(__name__), failure, error)
    assert failure.code == "unexpected"
    assert "private-token" not in str(failure.payload()) + caplog.text
    assert "private-password" not in str(failure.payload()) + caplog.text
    assert failure.diagnostic_id in caplog.text


def test_storage_error_has_no_automatic_retry_permission():
    failure = exception_failure(
        sqlite3.OperationalError("secret path"), source="api", stage="request"
    )
    assert failure.code == "storage_failed"
    assert failure.recovery == "none"
    assert "secret path" not in str(failure.payload())
