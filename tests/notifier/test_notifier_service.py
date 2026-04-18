import pytest
from pydantic import SecretStr

from xuanshu.core.enums import RunMode
from xuanshu.infra.notifier.telegram import TelegramNotifier, TextMessagePayload, render_text_message
from xuanshu.notifier.service import format_mode_change


def test_mode_change_notification_is_human_readable() -> None:
    assert format_mode_change(RunMode.REDUCE_ONLY) == "Mode changed to reduce-only"


def test_telegram_text_payload_is_typed() -> None:
    payload = render_text_message("hello")

    assert payload == TextMessagePayload(text="hello")
    assert payload.parse_mode is None


@pytest.mark.asyncio
async def test_telegram_notifier_send_text_makes_http_request() -> None:
    calls = []

    class _Client:
        async def post(self, url, json):
            calls.append((url, json))

    notifier = TelegramNotifier(
        bot_token=SecretStr("token"),
        chat_id="123",
        client=_Client(),
    )

    await notifier.send_text(TextMessagePayload(text="hello"))

    assert calls == [(
        "https://api.telegram.org/bottoken/sendMessage",
        {"chat_id": "123", "text": "hello"},
    )]
