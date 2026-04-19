import json
import logging

from xuanshu.ops.runtime_logging import configure_runtime_logger


def test_configure_runtime_logger_emits_json_to_stderr(capsys) -> None:
    logger = configure_runtime_logger("trader.test")

    logger.info("runtime_started", extra={"service": "trader", "mode": "halted"})

    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip())

    assert payload["event"] == "runtime_started"
    assert payload["service"] == "trader"
    assert payload["mode"] == "halted"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "trader.test"


def test_configure_runtime_logger_reuses_existing_logger_without_duplicate_handlers() -> None:
    logger_a = configure_runtime_logger("shared.test")
    logger_b = configure_runtime_logger("shared.test")

    assert logger_a is logger_b
    assert logger_a.level == logging.INFO
    assert len(logger_a.handlers) == 1
