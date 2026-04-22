from dataclasses import dataclass, field

import httpx

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class TextMessagePayload:
    text: str
    parse_mode: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramInboundMessage:
    update_id: int
    chat_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TelegramBotCommand:
    command: str
    description: str


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
        response = await self.client.post(
            f"https://api.telegram.org/bot{self.bot_token.get_secret_value()}/sendMessage",
            json=body,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()

    async def set_commands(self, commands: list[TelegramBotCommand]) -> None:
        response = await self.client.post(
            f"https://api.telegram.org/bot{self.bot_token.get_secret_value()}/setMyCommands",
            json={
                "commands": [
                    {"command": command.command, "description": command.description}
                    for command in commands
                ]
            },
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()

    async def fetch_updates(
        self,
        offset: int | None = None,
        limit: int = 20,
        timeout_sec: int = 30,
    ) -> list[TelegramInboundMessage]:
        params: dict[str, int] = {"limit": limit, "timeout": timeout_sec}
        if offset is not None:
            params["offset"] = offset
        response = await self.client.get(
            f"https://api.telegram.org/bot{self.bot_token.get_secret_value()}/getUpdates",
            params=params,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        payload = response.json() if hasattr(response, "json") else {}
        results = payload.get("result", []) if isinstance(payload, dict) else []
        messages: list[TelegramInboundMessage] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            text = message.get("text")
            update_id = item.get("update_id")
            if not isinstance(chat, dict) or not isinstance(text, str) or not isinstance(update_id, int):
                continue
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            messages.append(
                TelegramInboundMessage(
                    update_id=update_id,
                    chat_id=str(chat_id),
                    text=text,
                )
            )
        return messages
