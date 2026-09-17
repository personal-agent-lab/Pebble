"""共享 fixture：把实例持久目录指向临时路径，避免测试写入真实 .data。"""

from collections.abc import Iterator

import pytest

from server.config import Settings, get_settings


@pytest.fixture
def settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv("PEBBLE_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def model_catalog(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """模型目录不启动真实 CLI：测试默认可用 Auto 与当前配置型号。"""
    catalog = [{"value": "auto", "displayName": "Auto", "isEnabled": True}]

    async def fetch(self):
        configured = self.settings.qoder_model
        extra = [{"value": configured, "displayName": configured}] if configured else []
        return [*catalog, *extra]

    monkeypatch.setattr("server.agent.models.ModelCatalog._fetch", fetch)
    monkeypatch.setattr("server.agent.models.RETRY_DELAYS", ())
    return catalog
