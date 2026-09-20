"""生产装配：邮件与日历是可选服务，缺凭证时关掉对应功能，其余照常启动。"""

import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from server.config import get_settings
from server.main import create_production_app

OWNER = "owner@example.com"
ORIGIN = "https://pebble.example.ts.net"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    monkeypatch.setenv("PEBBLE_TOOL_PORT", str(free_port()))
    monkeypatch.setenv("PEBBLE_AUTH", "tailscale")
    monkeypatch.setenv("PEBBLE_ALLOWED_USERS", OWNER)
    monkeypatch.setenv("PEBBLE_PUBLIC_ORIGIN", ORIGIN)
    # 网关装配会改写这个环境变量；先登记，测试结束时恢复。
    monkeypatch.setenv("QODERCN_CONFIG_DIR", str(tmp_path / "config"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def tool_names(app) -> set[str]:
    return {tool.name for tool in app.state.agent.gateway.tools}


def test_starts_without_gmail_or_icloud_and_hides_their_tools(isolated):
    app = create_production_app()

    with TestClient(app, headers={"Tailscale-User-Login": OWNER}) as client:
        health = client.get("/api/health").json()
        created = app.state.tasks.create_task("只聊天")
        documents = client.get("/api/kb/documents")

    assert health["status"] == "ok"
    assert health["services"]["gmail"]["status"] == "unconfigured"
    assert health["services"]["calendar"]["status"] == "unconfigured"
    assert "iCloud" in health["services"]["calendar"]["detail"]
    assert created["goal"] == "只聊天"
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


def test_production_app_requires_tailscale_identity(isolated):
    app = create_production_app()

    with TestClient(app) as client:
        anonymous = client.get("/api/tasks")
        owner = client.get("/api/tasks", headers={"Tailscale-User-Login": OWNER})

    assert anonymous.status_code == 403
    assert owner.status_code == 200


@pytest.mark.parametrize("name,value", [("PEBBLE_ALLOWED_USERS", ""), ("PEBBLE_PUBLIC_ORIGIN", "")])
def test_refuses_to_start_without_access_configuration(isolated, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="PEBBLE_AUTH=off"):
        create_production_app()


def test_auth_off_is_explicit(isolated, monkeypatch):
    monkeypatch.setenv("PEBBLE_AUTH", "off")
    monkeypatch.setenv("PEBBLE_ALLOWED_USERS", "")
    get_settings.cache_clear()
    app = create_production_app()

    with TestClient(app) as client:
        assert client.get("/api/tasks").status_code == 200


def test_tool_endpoint_listens_only_on_its_own_loopback_port(isolated):
    app = create_production_app()
    port = get_settings().tool_port

    with TestClient(app, headers={"Tailscale-User-Login": OWNER}) as client:
        # 业务应用上没有工具端点：对外转发的端口访问不到它。
        via_business = client.post("/mcp/unknown", json={}, headers={"Origin": ORIGIN})
        via_tool_port = httpx.post(f"http://127.0.0.1:{port}/mcp/unknown", json={})

    assert via_business.status_code in (404, 405)
    assert via_business.text != "未知工具会话"
    assert via_tool_port.status_code == 404
    assert via_tool_port.text == "未知工具会话"
