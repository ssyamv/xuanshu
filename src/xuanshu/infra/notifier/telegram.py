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

    def build_text_message(self, text: str) -> TextMessagePayload:
        return render_text_message(text)
