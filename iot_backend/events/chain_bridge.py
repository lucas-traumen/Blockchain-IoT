"""Durable event-package to blockchain-api bridge.

The worker is optional and never runs in the MQTT callback.  A retry always
reuses the exact payload stored in ``chain_event_outbox``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from iot_backend.ingestion.store import Store

logger = logging.getLogger(__name__)


class ChainEventBridge:
    def __init__(self, store: Store, base_url: str, token: str, *, max_attempts: int = 8, interval_s: float = 1.0):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_attempts = max(1, int(max_attempts))
        self.interval_s = max(0.1, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hrc-chain-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _post(self, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}/api/safety-event",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {"error": body}
            return exc.code, parsed
        except (URLError, TimeoutError, OSError) as exc:
            return 0, {"error": str(exc)}

    def _attempt(self, row) -> None:
        event_id = row["event_id"]
        attempts = int(row["attempts"])
        if attempts >= self.max_attempts:
            self.store.update_chain_outbox(event_id, status="DEAD_LETTER", last_error="retry limit reached")
            return
        self.store.update_chain_outbox(event_id, status="SUBMITTED", attempts=attempts + 1, last_error=None)
        status, body = self._post(json.loads(row["payload_json"]))
        if status in {200, 201, 202} and body.get("success") is True:
            confirmed = (
                body.get("transactionStatus") == "confirmed"
                or status in {200, 201} and body.get("transactionStatus") == "confirmed"
            )
            self.store.update_chain_outbox(
                event_id,
                status="CONFIRMED" if confirmed else "SUBMITTED",
                transaction_hash=body.get("recordTransactionHash") or body.get("transactionHash"),
                block_number=body.get("recordBlockNumber") or body.get("blockNumber"),
                chain_data_hash=body.get("dataHash"),
                last_http_status=status,
                last_error=None,
                next_attempt_at=(
                    None if confirmed else (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
                ),
                confirmed_at=datetime.now(UTC).isoformat() if confirmed else None,
            )
            return
        retryable = status == 0 or status >= 500 or status == 429
        if retryable and attempts + 1 < self.max_attempts:
            delay = min(300.0, 2 ** min(attempts, 8))
            next_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            self.store.update_chain_outbox(event_id, status="RETRYABLE_ERROR", next_attempt_at=next_at, last_http_status=status or None, last_error=str(body.get("error", "chain request failed")))
        else:
            self.store.update_chain_outbox(event_id, status="DEAD_LETTER", last_http_status=status or None, last_error=str(body.get("error", "permanent chain request failure")))

    def _run(self) -> None:
        logger.info("chain event bridge started: %s", self.base_url)
        while not self._stop.wait(self.interval_s):
            try:
                for row in self.store.due_chain_outbox(10):
                    if self._stop.is_set():
                        break
                    self._attempt(row)
            except Exception:  # noqa: BLE001
                logger.exception("chain outbox worker failed")
        logger.info("chain event bridge stopped")
