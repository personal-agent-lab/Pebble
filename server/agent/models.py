"""当前 Qoder 账号可用模型目录。

目录经 SDK 的 `get_available_models()` 读取：CLI `--list-models` 只打印显示名，
托管型号的调用值（如 `qfmodel`）无法从中还原，自定义型号的 `value` 则是 UUID。

读取会受网络波动影响，所以区分两类结果：读不到目录是暂时的，沿用最近一次成功读到、
落盘保存的目录；读到了目录而型号不在其中才是确定的不可用。缓存判断有误时，
模型调用本身仍会明确失败，不会换用其他型号。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from qodercn_agent_sdk import QoderAgentOptions, QoderSDKClient, access_token

from server.config import Settings, default_model, get_settings
from server.errors import DependencyUnavailableError, ModelValidationError

logger = logging.getLogger(__name__)

CUSTOM_MODEL_ID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
CATALOG_TIMEOUT_SECONDS = 30
# 读取失败后的重试间隔；只重试这几次，仍失败就交给缓存兜底。
RETRY_DELAYS: tuple[float, ...] = (1.0,)
# 这么新的目录直接用于轮次校验，不再起 CLI 子进程。
TURN_FRESH_SECONDS = 60
# 目录展示超过这个时间就在后台刷新，页面仍立即拿到缓存。
REFRESH_AFTER_SECONDS = 5 * 60
# 读不到最新目录时，缓存在这个时间内仍可作为校验依据。
FALLBACK_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str
    kind: str

    def response(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "kind": self.kind}


def parse_models(raw: list[dict[str, Any]]) -> list[ModelEntry]:
    entries = []
    for item in raw:
        value = item.get("value")
        if not value or item.get("isEnabled") is False:
            continue
        entries.append(
            ModelEntry(
                id=value,
                label=item.get("displayName") or value,
                kind="custom" if CUSTOM_MODEL_ID.fullmatch(value) else "managed",
            )
        )
    return entries


class ModelCatalog:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.path = self.settings.data_dir / "agent" / "models.json"
        self._entries: list[ModelEntry] | None = None
        self._fetched_at: float | None = None
        # 最近一次刷新是否失败：页面据此提示目录可能不是最新。
        self._refresh_failed = False
        self._refreshing: asyncio.Future[list[ModelEntry]] | None = None
        self._background: set[asyncio.Task] = set()
        self._load()

    @property
    def default_model(self) -> str:
        return default_model(self.settings)

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = [ModelEntry(**item) for item in data["models"]]
            fetched_at = float(data["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if entries:
            self._entries, self._fetched_at = entries, fetched_at

    def _save(self) -> None:
        payload = {
            "fetched_at": self._fetched_at,
            "models": [entry.response() for entry in self._entries or []],
        }
        temporary = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            logger.warning("模型目录缓存写入失败", exc_info=True)

    def _age(self) -> float | None:
        return None if self._fetched_at is None else time.time() - self._fetched_at

    def _usable_cache(self) -> list[ModelEntry] | None:
        age = self._age()
        if self._entries is None or age is None or age > FALLBACK_MAX_AGE_SECONDS:
            return None
        return self._entries

    async def _fetch(self) -> list[dict[str, Any]]:
        token = self.settings.qoder_token
        if token is None:
            raise DependencyUnavailableError("未配置 Qoder 访问令牌")
        workspace = self.settings.data_dir / "agent" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        options = QoderAgentOptions(
            tools=[],
            setting_sources=[],
            cwd=workspace,
            auth=access_token(token.get_secret_value()),
        )
        async with QoderSDKClient(options) as client:
            return list(await client.get_available_models())

    async def _read(self) -> list[ModelEntry]:
        error: Exception | None = None
        for delay in (0.0, *RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                entries = parse_models(
                    await asyncio.wait_for(self._fetch(), CATALOG_TIMEOUT_SECONDS)
                )
            except Exception as failure:
                error = failure
                continue
            if entries:
                return entries
            error = DependencyUnavailableError("Qoder 模型目录为空")
        if isinstance(error, DependencyUnavailableError):
            raise error
        raise DependencyUnavailableError(f"无法读取 Qoder 模型目录：{error}") from error

    async def refresh(self) -> list[ModelEntry]:
        """读取最新目录；并发调用共享同一次读取，失败不覆盖已有缓存。"""
        refreshing = self._refreshing
        if refreshing is None or refreshing.done():
            refreshing = self._refreshing = asyncio.ensure_future(self._read())
        try:
            entries = await asyncio.shield(refreshing)
        except DependencyUnavailableError:
            self._refresh_failed = True
            raise
        if self._fetched_at is None or refreshing is self._refreshing:
            self._entries, self._fetched_at, self._refresh_failed = entries, time.time(), False
            self._save()
            self._refreshing = None
        return entries

    def refresh_in_background(self) -> None:
        if self._refreshing is not None and not self._refreshing.done():
            return

        async def run() -> None:
            try:
                await self.refresh()
            except DependencyUnavailableError as error:
                logger.warning("后台刷新模型目录失败：%s", error)

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    async def validate(self, model: str, *, new_task: bool = True) -> str:
        """新建任务按可用缓存即时判断；轮次校验复用一分钟内的目录，更旧就先读最新目录。

        新建任务不等目录读取：缓存里有该型号即通过，缓存超过一分钟就在后台刷新；
        型号若其实已下线，首轮校验会读到最新目录并让该轮明确失败。缓存里没有该型号
        或没有可用缓存时才同步读取。读不到最新目录时按缓存判断；新建任务连可用缓存
        也没有就报服务不可用，已有任务则放行，由模型调用本身给出结果。
        """
        age = self._age()
        cached = self._usable_cache()
        if new_task and cached is not None and model in {entry.id for entry in cached}:
            if age is not None and age >= TURN_FRESH_SECONDS:
                self.refresh_in_background()
            return model
        if (
            not new_task
            and self._entries is not None
            and age is not None
            and age < TURN_FRESH_SECONDS
        ):
            entries = self._entries
        else:
            try:
                entries = await self.refresh()
            except DependencyUnavailableError:
                entries = self._usable_cache()
                if entries is None:
                    if new_task:
                        raise
                    return model
        if model not in {entry.id for entry in entries}:
            raise ModelValidationError(model)
        return model

    async def response(self) -> dict:
        """展示目录：有缓存立即返回，过期或上次失败就后台刷新；从未读到过才等待读取。"""
        if self._entries is None:
            await self.refresh()
        else:
            age = self._age()
            if age is None or age > REFRESH_AFTER_SECONDS or self._refresh_failed:
                self.refresh_in_background()
        entries = self._entries or []
        fetched_at = datetime.fromtimestamp(self._fetched_at or 0, UTC).isoformat()
        return {
            "default_model": self.default_model,
            "models": [entry.response() for entry in entries],
            "fetched_at": fetched_at,
            "stale": self._refresh_failed,
        }
