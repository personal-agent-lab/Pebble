import logging
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.errors import install_error_handlers


def test_unexpected_http_error_has_safe_json_and_correlated_server_log(caplog):
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/broken")
    def broken():
        raise sqlite3.OperationalError("password=private-password")

    with caplog.at_level(logging.ERROR), TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/broken")
    assert response.status_code == 500
    payload = response.json()
    assert payload["error"] == "storage_failed"
    assert payload["failure"]["diagnostic_id"] in caplog.text
    assert "private-password" not in response.text + caplog.text
