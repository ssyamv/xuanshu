from pydantic import Field
from pydantic import field_validator
from pydantic.networks import AnyHttpUrl, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource, PydanticBaseSettingsSource


class _SettingsEnvSource(EnvSettingsSource):
    def prepare_field_value(self, field_name, field, value, value_is_complex):
        if field_name == "okx_symbols" and isinstance(value, str):
            return tuple(symbol.strip() for symbol in value.split(",") if symbol.strip())
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="XUANSHU_", extra="ignore")

    env: str = Field(default="dev", min_length=1)
    okx_symbols: tuple[str, ...] = Field(default=("BTC-USDT-SWAP", "ETH-USDT-SWAP"), min_length=1)
    redis_url: RedisDsn = Field(validation_alias="REDIS_URL")
    postgres_dsn: PostgresDsn = Field(validation_alias="POSTGRES_DSN")
    qdrant_url: AnyHttpUrl = Field(validation_alias="QDRANT_URL")
    ai_timeout_sec: int = Field(default=12, gt=0, le=300)

    @field_validator("okx_symbols", mode="before")
    @classmethod
    def parse_okx_symbols(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(symbol.strip() for symbol in value.split(",") if symbol.strip())
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        custom_env_settings = _SettingsEnvSource(
            settings_cls,
            case_sensitive=env_settings.case_sensitive,
            env_prefix=env_settings.env_prefix,
            env_prefix_target=env_settings.env_prefix_target,
            env_nested_delimiter=env_settings.env_nested_delimiter,
            env_nested_max_split=env_settings.env_nested_max_split,
            env_ignore_empty=env_settings.env_ignore_empty,
            env_parse_none_str=env_settings.env_parse_none_str,
            env_parse_enums=env_settings.env_parse_enums,
        )
        return init_settings, custom_env_settings, dotenv_settings, file_secret_settings
