"""服务端配置：实例持久目录与服务监听地址。

只定义骨架自身需要的键。SDK 模型解析、qodercli 工作目录与 Gmail 凭证的配置项
待第一阶段验证有实测结果后再补，避免现在猜名字导致后续重写。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PEBBLE_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = REPO_ROOT / ".data"
    host: str = "127.0.0.1"
    port: int = 8000

    gmail_credentials_path: Path | None = None
    gmail_token_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pebble.db"

    @property
    def gmail_credentials_file(self) -> Path:
        return self.gmail_credentials_path or (self.data_dir / "credentials.json")

    @property
    def gmail_token_file(self) -> Path:
        return self.gmail_token_path or (self.data_dir / "gmail_token.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()
