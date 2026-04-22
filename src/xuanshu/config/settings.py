from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic.networks import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource, PydanticBaseSettingsSource

from xuanshu.core.enums import OkxAccountMode, RunMode


class _SettingsEnvSource(EnvSettingsSource):
    def prepare_field_value(self, field_name, field, value, value_is_complex):
        if field_name == "okx_symbols" and isinstance(value, str):
            return tuple(symbol.strip() for symbol in value.split(",") if symbol.strip())
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class _SettingsDotEnvSource(DotEnvSettingsSource):
    def prepare_field_value(self, field_name, field, value, value_is_complex):
        if field_name == "okx_symbols" and isinstance(value, str):
            return tuple(symbol.strip() for symbol in value.split(",") if symbol.strip())
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class OkxRuntimeSecrets(BaseModel):
    api_key: SecretStr
    api_secret: SecretStr
    passphrase: SecretStr


class TelegramRuntimeSecrets(BaseModel):
    bot_token: SecretStr
    chat_id: str = Field(min_length=1)


class _XuanshuBaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="XUANSHU_", extra="ignore")

    @field_validator("okx_symbols", mode="before", check_fields=False)
    @classmethod
    def parse_okx_symbols(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(symbol.strip() for symbol in value.split(",") if symbol.strip())
        return value

    @field_validator(
        "okx_api_key",
        "okx_api_secret",
        "okx_api_passphrase",
        "telegram_bot_token",
        "telegram_chat_id",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def empty_runtime_values_are_none(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("okx_symbols", mode="after", check_fields=False)
    @classmethod
    def reject_blank_okx_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not symbol.strip() for symbol in value):
            raise ValueError("okx_symbols must not contain blank symbols")
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
        custom_dotenv_settings = _SettingsDotEnvSource(
            settings_cls,
            env_file=dotenv_settings.env_file,
            env_file_encoding=dotenv_settings.env_file_encoding,
            case_sensitive=dotenv_settings.case_sensitive,
            env_prefix=dotenv_settings.env_prefix,
            env_prefix_target=dotenv_settings.env_prefix_target,
            env_nested_delimiter=dotenv_settings.env_nested_delimiter,
            env_nested_max_split=dotenv_settings.env_nested_max_split,
            env_ignore_empty=dotenv_settings.env_ignore_empty,
            env_parse_none_str=dotenv_settings.env_parse_none_str,
            env_parse_enums=dotenv_settings.env_parse_enums,
        )
        return init_settings, custom_env_settings, custom_dotenv_settings, file_secret_settings


class Settings(_XuanshuBaseSettings):
    env: str = Field(default="dev", min_length=1)
    okx_symbols: tuple[str, ...] = Field(default=("BTC-USDT-SWAP", "ETH-USDT-SWAP"), min_length=1)
    okx_api_key: SecretStr | None = Field(default=None, validation_alias="OKX_API_KEY")
    okx_api_secret: SecretStr | None = Field(default=None, validation_alias="OKX_API_SECRET")
    okx_api_passphrase: SecretStr | None = Field(default=None, validation_alias="OKX_API_PASSPHRASE")
    telegram_bot_token: SecretStr | None = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")
    redis_url: RedisDsn = Field(validation_alias="REDIS_URL")
    postgres_dsn: PostgresDsn = Field(validation_alias="POSTGRES_DSN")

    def require_trader_runtime(self) -> OkxRuntimeSecrets:
        return OkxRuntimeSecrets(
            api_key=self.okx_api_key,
            api_secret=self.okx_api_secret,
            passphrase=self.okx_api_passphrase,
        )

    def require_notifier_runtime(self) -> TelegramRuntimeSecrets:
        return TelegramRuntimeSecrets(
            bot_token=self.telegram_bot_token,
            chat_id=self.telegram_chat_id,
        )


class TraderRuntimeSettings(_XuanshuBaseSettings):
    okx_symbols: tuple[str, ...] = Field(default=("BTC-USDT-SWAP", "ETH-USDT-SWAP"), min_length=1)
    trader_starting_nav: float = Field(default=250_000.0, gt=0.0)
    default_run_mode: RunMode = Field(default=RunMode.NORMAL)
    okx_account_mode: OkxAccountMode = Field(default=OkxAccountMode.LIVE)
    fixed_strategy_snapshot_path: str | None = Field(default=None)
    redis_url: RedisDsn = Field(default="redis://redis:6379/0", validation_alias="REDIS_URL")
    postgres_dsn: PostgresDsn = Field(
        default="postgresql+psycopg://xuanshu:xuanshu@postgres:5432/xuanshu",
        validation_alias="POSTGRES_DSN",
    )
    okx_api_key: SecretStr = Field(validation_alias="OKX_API_KEY")
    okx_api_secret: SecretStr = Field(validation_alias="OKX_API_SECRET")
    okx_api_passphrase: SecretStr = Field(validation_alias="OKX_API_PASSPHRASE")


class NotifierRuntimeSettings(_XuanshuBaseSettings):
    okx_symbols: tuple[str, ...] = Field(default=("BTC-USDT-SWAP", "ETH-USDT-SWAP"), min_length=1)
    telegram_bot_token: SecretStr = Field(validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(min_length=1, validation_alias="TELEGRAM_CHAT_ID")
    redis_url: RedisDsn = Field(default="redis://redis:6379/0", validation_alias="REDIS_URL")
    postgres_dsn: PostgresDsn = Field(
        default="postgresql+psycopg://xuanshu:xuanshu@postgres:5432/xuanshu",
        validation_alias="POSTGRES_DSN",
    )


class DashboardRuntimeSettings(_XuanshuBaseSettings):
    okx_symbols: tuple[str, ...] = Field(default=("BTC-USDT-SWAP", "ETH-USDT-SWAP"), min_length=1)
    redis_url: RedisDsn = Field(default="redis://redis:6379/0", validation_alias="REDIS_URL")
    postgres_dsn: PostgresDsn = Field(
        default="postgresql+psycopg://xuanshu:xuanshu@postgres:5432/xuanshu",
        validation_alias="POSTGRES_DSN",
    )
