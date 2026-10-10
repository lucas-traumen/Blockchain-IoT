"""Small HTTP bridge from IoT session outcomes to blockchain_api.

The IoT service never holds a wallet key.  blockchain_api remains the only
component allowed to sign reward-contract transactions; this client sends the
already evaluated outcome with a deterministic evidence hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

__all__ = ["RewardSettlementError", "RewardClient", "build_reward_client", "evidence_hash"]


class RewardSettlementError(RuntimeError):
    """Blockchain API could not accept or settle the session."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def evidence_hash(session: dict[str, Any], stats: dict[str, Any], result: dict[str, Any]) -> str:
    """Hash the session identity, statistics and deterministic policy result."""
    document = {"session": session, "stats": stats, "result": result}
    encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "0x" + hashlib.sha256(encoded).hexdigest()


class RewardClient:
    def __init__(
        self,
        base_url: str,
        ingest_token: str,
        beneficiary: str,
        *,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.ingest_token = ingest_token
        self.beneficiary = beneficiary
        self.timeout_s = timeout_s

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.ingest_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise RewardSettlementError("blockchain_api returned a non-object response", status_code=response.status)
                return data
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body_data = json.loads(raw)
            except ValueError:
                body_data = raw
            message = body_data.get("error", str(exc)) if isinstance(body_data, dict) else str(exc)
            raise RewardSettlementError(message, status_code=exc.code, body=body_data) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RewardSettlementError(f"blockchain_api unavailable: {exc}") from exc

    def start_session(self, session_id: str) -> dict[str, Any]:
        return self._post("/api/session-start", {
            "sessionId": session_id,
            "beneficiary": self.beneficiary,
        })

    def settle(
        self,
        session_id: str,
        session: dict[str, Any],
        stats: dict[str, Any],
        result: dict[str, Any],
        *,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        timestamp = int(datetime.now(UTC).timestamp())
        return self._post("/api/session-outcome", {
            "sessionId": session_id,
            "beneficiary": self.beneficiary,
            "outcome": result["outcome"],
            "evidenceHash": evidence or evidence_hash(session, stats, result),
            "timestamp": timestamp,
        })


def build_reward_client(settings: Any) -> RewardClient | None:
    """Build the bridge only when all non-secret routing settings are present."""
    if not settings.reward_api_url or not settings.reward_ingest_token or not settings.reward_beneficiary:
        return None
    return RewardClient(
        settings.reward_api_url,
        settings.reward_ingest_token,
        settings.reward_beneficiary,
        timeout_s=settings.reward_timeout_s,
    )
