from dataclasses import dataclass

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class TextMessagePayload:
    text: str
    parse_mode: str | None = None


def render_text_message(text: str) -> TextMessagePayload:
    return TextMessagePayload(text=text)


@dataclass(frozen=True, slots=True)
class TelegramNotifier:
    bot_token: SecretStr
    chat_id: str

    async def send_text(self, text: str) -> None:
        render_text_message(text)
        return None
