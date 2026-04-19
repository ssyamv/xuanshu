from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
import json
import subprocess
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from xuanshu.infra.ai.governor_client import _extract_json_object, _extract_response_text

_DEFAULT_RESEARCH_MODEL = "gpt-4.1-mini"
_RESPONSES_API_URL = "https://api.openai.com/v1/responses"
_RESEARCH_PROVIDER_INSTRUCTIONS = (
    "You are a strategy research helper inside Governor. "
    "Return exactly one JSON object describing research analysis assistance only. "
    "Do not suggest executable trading actions. Return JSON only."
)


class ResearchProviderName(StrEnum):
    API = "api"
    CODEX_CLI = "codex_cli"


class ResearchProviderSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis: str = Field(min_length=1)
    strategy_family: str = Field(min_length=1)
    entry_signal: str = Field(min_length=1)
    exit_stop_loss_bps: int = Field(gt=0)
    exit_take_profit_bps: int = Field(gt=0)
    risk_fraction: float = Field(gt=0.0)
    max_hold_minutes: int = Field(gt=0)
    failure_modes: list[str] = Field(default_factory=list)
    invalidating_conditions: list[str] = Field(default_factory=list)


class ResearchProvider(Protocol):
    provider_name: ResearchProviderName

    async def generate_analysis(
        self,
        *,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> ResearchProviderSuggestion:
        ...


class ApiResearchProvider:
    provider_name = ResearchProviderName.API

    def __init__(self, *, api_key: SecretStr, timeout_sec: int) -> None:
        self.api_key = api_key
        self.timeout_sec = timeout_sec

    async def generate_analysis(
        self,
        *,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> ResearchProviderSuggestion:
        payload = {
            "model": _DEFAULT_RESEARCH_MODEL,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": _RESEARCH_PROVIDER_INSTRUCTIONS}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Research context JSON:\n"
                                + json.dumps(
                                    {
                                        "symbol_scope": symbol_scope,
                                        "market_environment": market_environment,
                                        "historical_rows": _canonicalize_historical_rows(historical_rows),
                                        "research_reason": research_reason,
                                    },
                                    ensure_ascii=True,
                                    sort_keys=True,
                                )
                            ),
                        }
                    ],
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=float(self.timeout_sec), headers=headers) as client:
            response = await client.post(_RESPONSES_API_URL, json=payload)
            response.raise_for_status()
        return _parse_suggestion_payload(response.json())


class CodexCliResearchProvider:
    provider_name = ResearchProviderName.CODEX_CLI

    def __init__(self, *, command: str = "codex", cwd: str | None = None) -> None:
        self.command = command
        self.cwd = cwd

    async def generate_analysis(
        self,
        *,
        symbol_scope: list[str],
        market_environment: str,
        historical_rows: list[dict[str, object]],
        research_reason: str,
    ) -> ResearchProviderSuggestion:
        prompt = (
            f"{_RESEARCH_PROVIDER_INSTRUCTIONS}\n"
            f"Research context JSON:\n"
            f"{json.dumps({'symbol_scope': symbol_scope, 'market_environment': market_environment, 'historical_rows': _canonicalize_historical_rows(historical_rows), 'research_reason': research_reason}, ensure_ascii=True, sort_keys=True)}"
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
        return _parse_suggestion_payload(completed.stdout)


def create_research_provider(
    *,
    provider_name: ResearchProviderName | str,
    openai_api_key: SecretStr | None,
    timeout_sec: int,
    codex_command: str = "codex",
    codex_cwd: str | None = None,
) -> ResearchProvider:
    try:
        resolved = (
            provider_name
            if isinstance(provider_name, ResearchProviderName)
            else ResearchProviderName(str(provider_name).strip())
        )
    except ValueError as exc:
        raise ValueError(f"unsupported research provider: {provider_name}") from exc

    if resolved == ResearchProviderName.API:
        if openai_api_key is None:
            raise ValueError("openai_api_key is required for api research provider")
        return ApiResearchProvider(api_key=openai_api_key, timeout_sec=timeout_sec)
    if resolved == ResearchProviderName.CODEX_CLI:
        return CodexCliResearchProvider(command=codex_command, cwd=codex_cwd)
    raise ValueError(f"unsupported research provider: {provider_name}")


def _parse_suggestion_payload(payload: object) -> ResearchProviderSuggestion:
    if isinstance(payload, Mapping):
        text = _extract_response_text(payload)
        if text is not None:
            return ResearchProviderSuggestion.model_validate_json(_extract_json_object(text))
    if isinstance(payload, str):
        return ResearchProviderSuggestion.model_validate_json(_extract_json_object(payload))
    raise RuntimeError("research provider response did not contain valid JSON")


def _canonicalize_historical_rows(historical_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    canonical_rows: list[dict[str, object]] = []
    for row in historical_rows:
        timestamp = row.get("timestamp")
        canonical_rows.append(
            {
                "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else None,
                "close": row.get("close"),
            }
        )
    return canonical_rows
