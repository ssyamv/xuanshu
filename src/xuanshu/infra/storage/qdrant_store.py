from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx

QDRANT_COLLECTIONS = (
    "market_case",
    "risk_case",
    "governance_case",
)


class QdrantCaseStore:
    def __init__(
        self,
        qdrant_url: str = "http://qdrant:6333",
        client: httpx.Client | object | None = None,
    ) -> None:
        self.qdrant_url = qdrant_url.rstrip("/")
        self._client = client or httpx.Client(timeout=5.0)

    def search_governance_cases(
        self,
        query: dict[str, object],
        limit: int = 3,
    ) -> list[dict[str, object]]:
        request_payload = {
            "limit": limit,
            "with_payload": True,
            "with_vectors": False,
            "filter": {
                "must": self._build_filter_clauses(query),
            },
        }
        try:
            response = self._client.post(
                f"{self.qdrant_url}/collections/governance_case/points/scroll",
                json=request_payload,
            )
            response.raise_for_status()
        except Exception:
            return []
        payload = response.json()
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        points = result.get("points", []) if isinstance(result, dict) else []
        cases: list[dict[str, object]] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            case_payload = point.get("payload")
            if isinstance(case_payload, dict):
                cases.append(deepcopy(case_payload))
        return cases

    def _build_filter_clauses(self, query: dict[str, object]) -> list[dict[str, object]]:
        clauses: list[dict[str, object]] = []
        trigger_reason = query.get("trigger_reason")
        if isinstance(trigger_reason, str) and trigger_reason:
            clauses.append({"key": "trigger_reason", "match": {"value": trigger_reason}})
        current_run_mode = query.get("current_run_mode")
        if isinstance(current_run_mode, str) and current_run_mode:
            clauses.append({"key": "current_run_mode", "match": {"value": current_run_mode}})
        recommended_mode_floor = query.get("recommended_mode_floor")
        if isinstance(recommended_mode_floor, str) and recommended_mode_floor:
            clauses.append(
                {"key": "recommended_mode_floor", "match": {"value": recommended_mode_floor}}
            )
        active_fault_flags = query.get("active_fault_flags")
        if isinstance(active_fault_flags, list):
            flag_values = [flag for flag in active_fault_flags if isinstance(flag, str) and flag]
            if flag_values:
                clauses.append({"key": "active_fault_flags", "match": {"any": flag_values}})
        return clauses
