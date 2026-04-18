from xuanshu.core.enums import RunMode
from xuanshu.infra.notifier.telegram import TextMessagePayload, render_text_message
from xuanshu.notifier.service import format_mode_change


def test_mode_change_notification_is_human_readable() -> None:
    assert format_mode_change(RunMode.REDUCE_ONLY) == "Mode changed to reduce-only"


def test_telegram_text_payload_is_typed() -> None:
    payload = render_text_message("hello")

    assert payload == TextMessagePayload(text="hello")
    assert payload.parse_mode is None
