"""SDK 模型目录的解析、落盘缓存与网络波动下的校验。"""

import asyncio
import time

import pytest

from server.agent import models
from server.agent.models import ModelCatalog
from server.errors import DependencyUnavailableError, ModelValidationError


def test_parses_managed_and_custom_models(settings, monkeypatch):
    raw = [
        {"value": "auto", "displayName": "Auto", "isEnabled": True},
        {"value": "qfmodel", "displayName": "Qwen3.8-Flash", "isEnabled": True},
        {"value": "off", "displayName": "停用", "isEnabled": False},
        {"value": "12345678-1234-1234-1234-123456789abc", "displayName": "我的模型"},
    ]

    async def fetch(self):
        return raw

    monkeypatch.setattr(ModelCatalog, "_fetch", fetch)
    response = asyncio.run(ModelCatalog(settings=settings).response())

    assert response["default_model"] == (settings.qoder_model or "auto")
    assert response["stale"] is False
    assert response["models"] == [
        {"id": "auto", "label": "Auto", "kind": "managed"},
        {"id": "qfmodel", "label": "Qwen3.8-Flash", "kind": "managed"},
        {"id": "12345678-1234-1234-1234-123456789abc", "label": "我的模型", "kind": "custom"},
    ]


class Flaky:
    """可切换成功/失败的目录来源，并记录读取次数。"""

    def __init__(self, monkeypatch):
        self.ok = True
        self.calls = 0

        async def fetch(catalog):
            self.calls += 1
            await asyncio.sleep(0)
            if not self.ok:
                raise ConnectionError("network down")
            return [{"value": "auto", "displayName": "Auto"}, {"value": "qfmodel"}]

        monkeypatch.setattr(ModelCatalog, "_fetch", fetch)


def test_no_catalog_ever_fails_creation_but_lets_turns_through(settings, monkeypatch):
    source = Flaky(monkeypatch)
    source.ok = False
    catalog = ModelCatalog(settings=settings)

    with pytest.raises(DependencyUnavailableError, match="无法读取 Qoder 模型目录"):
        asyncio.run(catalog.validate("auto"))
    assert asyncio.run(catalog.validate("auto", fresh=False)) == "auto"


def test_network_failure_falls_back_to_persisted_catalog(settings, monkeypatch):
    source = Flaky(monkeypatch)
    asyncio.run(ModelCatalog(settings=settings).refresh())

    # 新实例（等同重启）从磁盘读到目录；此后网络失败仍按缓存判断。
    source.ok = False
    catalog = ModelCatalog(settings=settings)
    assert asyncio.run(catalog.validate("qfmodel")) == "qfmodel"
    with pytest.raises(ModelValidationError):
        asyncio.run(catalog.validate("gone"))

    response = asyncio.run(catalog.response())
    assert [entry["id"] for entry in response["models"]] == ["auto", "qfmodel"]
    assert response["stale"] is True

    # 缓存超过兜底期限后不再作为依据。
    catalog._fetched_at = time.time() - models.FALLBACK_MAX_AGE_SECONDS - 1
    with pytest.raises(DependencyUnavailableError):
        asyncio.run(catalog.validate("qfmodel"))


def test_turns_reuse_recent_catalog_and_refreshes_are_shared(settings, monkeypatch):
    source = Flaky(monkeypatch)
    catalog = ModelCatalog(settings=settings)

    async def concurrent():
        return await asyncio.gather(*(catalog.refresh() for _ in range(5)))

    asyncio.run(concurrent())
    assert source.calls == 1
    asyncio.run(catalog.validate("qfmodel", fresh=False))
    assert source.calls == 1
    asyncio.run(catalog.validate("qfmodel"))
    assert source.calls == 2


def test_failure_retries_before_giving_up(settings, monkeypatch):
    source = Flaky(monkeypatch)
    source.ok = False
    monkeypatch.setattr(models, "RETRY_DELAYS", (0.0, 0.0))
    with pytest.raises(DependencyUnavailableError):
        asyncio.run(ModelCatalog(settings=settings).refresh())
    assert source.calls == 3
