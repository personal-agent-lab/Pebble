"""生产装配：邮件与日历是可选服务，缺凭证时关掉对应功能，其余照常启动。"""

import pytest
from fastapi.testclient import TestClient

from server.config import get_settings
from server.main import create_production_app


@pytest.fixture
def isolated(settings, monkeypatch, tmp_path):
    """不读仓库根 .env 里的真实凭证：每项都显式指向临时目录或留空。"""
    monkeypatch.setenv("PEBBLE_GMAIL_CREDENTIALS_PATH", str(tmp_path / "missing-credentials.json"))
    monkeypatch.setenv("PEBBLE_GMAIL_TOKEN_PATH", str(tmp_path / "missing-token.json"))
    for name in (
        "PEBBLE_ICLOUD_ACCOUNT",
        "PEBBLE_ICLOUD_PASSWORD_PATH",
        "PEBBLE_ICLOUD_CALENDAR_URL",
    ):
        monkeypatch.setenv(name, "")
    # 网关装配会改写这个环境变量；先登记，测试结束时恢复。
    monkeypatch.setenv("QODERCN_CONFIG_DIR", str(tmp_path / "config"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def tool_names(app) -> set[str]:
    return {tool.name for tool in app.state.agent.gateway.tools}


def test_starts_without_gmail_or_icloud_and_hides_their_tools(isolated):
    app = create_production_app()

    with TestClient(app) as client:
        health = client.get("/api/health").json()
        created = client.post("/api/tasks", json={"goal": "只聊天"})
        documents = client.get("/api/kb/documents")

    assert health["status"] == "ok"
    assert health["services"]["gmail"]["status"] == "unconfigured"
    assert health["services"]["calendar"]["status"] == "unconfigured"
    assert "iCloud" in health["services"]["calendar"]["detail"]
    assert created.status_code == 201
    assert documents.status_code == 200

    names = tool_names(app)
    # 邮件与日历的工具（包括发不出去的草稿工具）都不交给模型；资料库照常可用
    assert not {name for name in names if name.startswith(("gmail_", "calendar_"))}
    assert {"kb_search", "kb_save"} <= names
    assert app.state.mail_source is None
    assert app.state.confirmations.send_message is None
    assert app.state.confirmations.create_event is None


def test_configured_services_are_wired_in(isolated, monkeypatch):
    # 只放凭证文件与配置，不启动应用生命周期：真实客户端在首次调用时才连接外部服务。
    (isolated / "credentials.json").write_text("{}", encoding="utf-8")
    (isolated / "icloud-password").write_text("app-password", encoding="utf-8")
    monkeypatch.setenv("PEBBLE_GMAIL_CREDENTIALS_PATH", str(isolated / "credentials.json"))
    monkeypatch.setenv("PEBBLE_ICLOUD_ACCOUNT", "owner@icloud.com")
    monkeypatch.setenv("PEBBLE_ICLOUD_PASSWORD_PATH", str(isolated / "icloud-password"))
    monkeypatch.setenv(
        "PEBBLE_ICLOUD_CALENDAR_URL", "https://p01-caldav.icloud.com/123/calendars/home/"
    )
    get_settings.cache_clear()

    app = create_production_app()

    assert app.state.services == {
        "gmail": {"status": "ok", "detail": None},
        "calendar": {"status": "ok", "detail": None},
    }
    names = tool_names(app)
    assert {"gmail_search", "gmail_prepare_email", "calendar_create_event"} <= names
    assert app.state.mail_source is not None
    assert app.state.confirmations.send_message is not None
