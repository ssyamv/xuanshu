from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="XUANSHU_", extra="ignore")

    env: str = "dev"
    okx_symbols: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    redis_url: str = Field(alias="REDIS_URL")
    postgres_dsn: str = Field(alias="POSTGRES_DSN")
    qdrant_url: str = Field(alias="QDRANT_URL")
    ai_timeout_sec: int = 12
