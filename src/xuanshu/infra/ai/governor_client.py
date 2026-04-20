from dataclasses import dataclass
from collections.abc import Mapping
import json
import subprocess
from typing import Protocol

import httpx
from pydantic import SecretStr

from xuanshu.contracts.strategy import StrategyConfigSnapshot

_DEFAULT_GOVERNOR_MODEL = "gpt-4.1-mini"
_RESPONSES_API_URL = "https://api.openai.com/v1/responses"
_GOVERNOR_INSTRUCTIONS = (
    "You are the Governor Service for a live trading system. "
    "Given a state summary, return exactly one JSON object matching this schema and nothing else: "
    '{"version_id": string, "generated_at": RFC3339 string, "effective_from": RFC3339 string, '
    '"expires_at": RFC3339 string, "symbol_whitelist": non-empty string[], '
    '"strategy_enable_flags": object<string, boolean>, "risk_multiplier": number, '
    '"per_symbol_max_position": number, "max_leverage": integer, '
    '"market_mode": "normal"|"degraded"|"reduce_only"|"halted", '
    '"approval_state": "approved"|"rejected", "source_reason": string, "ttl_sec": integer}. '
    "If state_summary contains symbol_summaries, derive symbol_whitelist from those symbols and never return an empty list. "
    "Do not return keys outside this schema. "
    "Return JSON only with no commentary."
)


class GovernorAgentRunner(Protocol):
    async def run(self, state_summary: Mapping[str, object]) -> object:
        ...


@dataclass(frozen=True, slots=True)
class ConfiguredGovernorAgentRunner:
    api_key: SecretStr
    timeout_sec: int

    async def run(self, state_summary: Mapping[str, object]) -> object:
        payload = {
            "model": _DEFAULT_GOVERNOR_MODEL,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _GOVERNOR_INSTRUCTIONS,
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"State summary JSON:\n{json.dumps(state_summary, ensure_ascii=True, sort_keys=True)}",
                        }
                    ],
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=float(self.timeout_sec), headers=headers) as client:
                response = await client.post(_RESPONSES_API_URL, json=payload)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RuntimeError("Governor AI request timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Governor AI request failed: {exc}") from exc

        text = _extract_response_text(response.json())
        if text is None:
            raise RuntimeError("Governor AI response did not contain text output")

        try:
            return json.loads(_extract_json_object(text))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Governor AI response did not contain valid JSON") from exc


@dataclass(frozen=True, slots=True)
class CodexCliGovernorAgentRunner:
    command: str = "codex"
    cwd: str | None = None

    async def run(self, state_summary: Mapping[str, object]) -> object:
        prompt = (
            f"{_GOVERNOR_INSTRUCTIONS}\n"
            f"State summary JSON:\n"
            f"{json.dumps(state_summary, ensure_ascii=True, sort_keys=True)}"
        )
        completed = subprocess.run(
            [self.command, "exec", "--skip-git-repo-check", prompt],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.cwd,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(stderr or "codex exec failed")
        try:
            return json.loads(_extract_json_object(completed.stdout))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Governor codex response did not contain valid JSON") from exc


class GovernorClient:
    def __init__(self, agent_runner: GovernorAgentRunner) -> None:
        self.agent_runner = agent_runner

    async def generate_snapshot(self, state_summary: Mapping[str, object]) -> StrategyConfigSnapshot:
        result = await self.agent_runner.run(state_summary)
        if isinstance(result, dict):
            symbol_whitelist = result.get("symbol_whitelist")
            if not isinstance(symbol_whitelist, list) or not symbol_whitelist:
                backfilled_symbols = _extract_symbol_scope(state_summary)
                if backfilled_symbols:
                    result = {**result, "symbol_whitelist": backfilled_symbols}
            result = _normalize_snapshot_bounds(result)
        return StrategyConfigSnapshot.model_validate(result)


def _extract_response_text(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = payload.get("output")
    if not isinstance(output, list):
        return None

    text_chunks: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                text_chunks.append(text.strip())
    if not text_chunks:
        return None
    return "\n".join(text_chunks)


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        while lines and lines[-1].strip() == "```":
            lines.pop()
        stripped = "\n".join(lines).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return stripped
    return stripped[start : end + 1]


def _extract_symbol_scope(state_summary: Mapping[str, object]) -> list[str]:
    symbol_summaries = state_summary.get("symbol_summaries")
    if not isinstance(symbol_summaries, list):
        return []
    symbols: list[str] = []
    for summary in symbol_summaries:
        if not isinstance(summary, Mapping):
            continue
        symbol = summary.get("symbol")
        if isinstance(symbol, str) and symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _normalize_snapshot_bounds(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    for field_name in ("risk_multiplier", "per_symbol_max_position"):
        value = normalized.get(field_name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            normalized[field_name] = min(max(float(value), 0.0), 1.0)
    value = normalized.get("max_leverage")
    if isinstance(value, int | float) and not isinstance(value, bool):
        normalized["max_leverage"] = max(1, min(int(value), 3))
    value = normalized.get("ttl_sec")
    if isinstance(value, int | float) and not isinstance(value, bool):
        normalized["ttl_sec"] = max(1, int(value))
    return normalized
