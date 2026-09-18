"""服务端配置：凭证只从环境或本机 .env 读取，不进入模型上下文。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
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

    qoder_model: str | None = None
    qoder_token: SecretStr | None = Field(
        default=None, validation_alias="QODERCN_PERSONAL_ACCESS_TOKEN"
    )
    # 轻量调用（任务标题、资料说明等一次性短文本）用自有 API Key 的低价模型；
    # 供应商、密钥、型号都不配时沿用托管的 qoder_model，主对话与记忆调用不受影响。
    light_model: str | None = None
    light_model_provider: str | None = None
    light_model_api_key: SecretStr | None = None
    light_model_base_url: str | None = None

    # 后台记忆回顾：每完成多少个用户消息轮触发一次；开关只管自动触发，手动接口不受限。
    memory_review_interval: int = Field(default=5, ge=1)
    memory_review_enabled: bool = True

    gmail_credentials_path: Path | None = None
    gmail_token_path: Path | None = None

    icloud_account: str | None = None
    icloud_password_path: Path | None = None
    icloud_calendar_url: str | None = None

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


def default_model(settings: Settings | None = None) -> str:
    """未指定型号时的服务端有效模型：配置值，否则为 Auto。"""
    return (settings or get_settings()).qoder_model or "auto"
