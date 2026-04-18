from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextMessagePayload:
    text: str
    parse_mode: str | None = None


def render_text_message(text: str) -> TextMessagePayload:
    return TextMessagePayload(text=text)
