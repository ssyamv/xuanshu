from dataclasses import dataclass, field

import httpx

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class TextMessagePayload:
    text: str
    parse_mode: str | None = None


def render_text_message(text: str) -> TextMessagePayload:
    return TextMessagePayload(text=text)


@dataclass(slots=True)
class TelegramNotifier:
    bot_token: SecretStr
    chat_id: str
    client: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)

    async def send_text(self, payload: TextMessagePayload) -> None:
        body = {"chat_id": self.chat_id, "text": payload.text}
        if payload.parse_mode is not None:
            body["parse_mode"] = payload.parse_mode
        await self.client.post(
            f"https://api.telegram.org/bot{self.bot_token.get_secret_value()}/sendMessage",
            json=body,
        )
