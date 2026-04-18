from pydantic import Field
from pydantic.networks import AnyHttpUrl, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="XUANSHU_", extra="ignore")

    env: str = Field(default="dev", min_length=1)
    okx_symbols: tuple[str, ...] = Field(default=("BTC-USDT-SWAP", "ETH-USDT-SWAP"), min_length=1)
    redis_url: RedisDsn = Field(validation_alias="REDIS_URL")
    postgres_dsn: PostgresDsn = Field(validation_alias="POSTGRES_DSN")
    qdrant_url: AnyHttpUrl = Field(validation_alias="QDRANT_URL")
    ai_timeout_sec: int = Field(default=12, gt=0, le=300)
