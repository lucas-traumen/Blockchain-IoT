"""Interactive hardware dataset collector with MQTT-backed session labels.

The collector talks to the running iot_backend HTTP API.  The iot_backend sends one
``session_start`` and one ``session_stop`` control command to the ESP32 for
 each run, waits for the device boundary ACK, and assigns telemetry to the
session.  This keeps the collector out of SQLite and avoids a second DB writer
when the Docker iot_backend is running.

Examples::

    # Interactive: choose one scenario at a time.
    .venv/bin/python -m scripts.collect_dataset

    # One interactive run with labels supplied on the command line.
    .venv/bin/python -m scripts.collect_dataset \
        --scenario approach --target-type person --speed-level slow

    # Repeat a pre-declared plan.  Each entry still pauses before START.
    .venv/bin/python -m scripts.collect_dataset \
        --plan configs/collection.example.yaml

    # Automation/CI style; every plan entry needs duration_s.
    .venv/bin/python -m scripts.collect_dataset \
        --plan configs/collection.example.yaml --non-interactive
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_CONFIG_PATH = REPO_ROOT / "configs" / "model.yaml"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "telemetry_v1.schema.json"
DEFAULT_API_URL = os.environ.get("HRC_API_URL", "http://127.0.0.1:8001")
DEFAULT_DEVICE_ID = os.environ.get("DEVICE_ID", "esp32_01")
DEFAULT_SETTLE_TIMEOUT_S = 8.0
DEFAULT_POLL_S = 0.25
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class CollectionError(RuntimeError):
    """A collector error that can be shown without a Python traceback."""


class ClosedRunError(CollectionError):
    """Failed collection whose owned session is confirmed closed by the API."""


class ApiError(CollectionError):
    """HTTP/API failure with method and path context."""

    def __init__(self, method: str, path: str, message: str, status: int | None = None):
        self.method = method
        self.path = path
        self.status = status
        super().__init__(f"{method} {path}: {message}")


@dataclass(frozen=True)
class SessionSpec:
    """One physical collection run and its labels."""

    scenario: str
    target_type: str
    speed_level: str | None = None
    duration_s: float | None = None
    zone_note: str | None = None
    session_id: str | None = None

    def validate(self) -> None:
        if not self.scenario.strip():
            raise CollectionError("scenario không được rỗng")
        if not self.target_type.strip():
            raise CollectionError("target_type không được rỗng")
        if self.duration_s is not None and (not math.isfinite(self.duration_s) or self.duration_s <= 0):
            raise CollectionError("duration_s phải > 0 hoặc bỏ trống để dừng bằng Enter")
        if self.session_id is not None:
            validate_session_id(self.session_id)


@dataclass
class CollectedSession:
    """Persisted result and quality summary for one closed session."""

    session_id: str
    scenario: str
    target_type: str
    speed_level: str | None
    output_file: str
    rows: int
    quality_ok: bool
    quality_issues: list[str]
    boundary: dict[str, Any]
    quality: dict[str, Any]
    dataset_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionApi(Protocol):
    """Small API seam; tests can use a deterministic fake without a broker."""

    def health(self) -> dict[str, Any]: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def start_session(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    def stop_session(self, session_id: str) -> dict[str, Any]: ...

    def get_session(self, session_id: str) -> dict[str, Any]: ...

    def export_session(self, session_id: str) -> list[dict[str, Any]]: ...


class HttpSessionApi:
    """HTTP implementation for the iot_backend session API."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 10.0,
        opener: Callable[..., Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = opener or urllib.request.urlopen

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            response = self._opener(request, timeout=self.timeout_s)
            with response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            detail = _response_detail(raw)
            raise ApiError(method, path, detail, exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(method, path, str(exc)) from exc
        if status < 200 or status >= 300:
            raise ApiError(method, path, _response_detail(raw), status)
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(method, path, "response không phải JSON hợp lệ") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/sessions")

    def start_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/sessions/start", payload)

    def stop_session(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/sessions/{_url_segment(session_id)}/stop")

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{_url_segment(session_id)}")

    def export_session(self, session_id: str) -> list[dict[str, Any]]:
        path = f"/sessions/{_url_segment(session_id)}/export?format=jsonl"
        raw = self._request_raw("GET", path)
        rows: list[dict[str, Any]] = []
        for line_no, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ApiError("GET", path, f"dòng {line_no} không phải JSON") from exc
            if not isinstance(item, dict):
                raise ApiError("GET", path, f"dòng {line_no} không phải object")
            rows.append(item)
        return rows

    def _request_raw(self, method: str, path: str) -> str:
        request = urllib.request.Request(
            f"{self.base_url}{path}", headers={"Accept": "application/x-ndjson"}, method=method
        )
        try:
            response = self._opener(request, timeout=self.timeout_s)
            with response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raise ApiError(method, path, _response_detail(exc.read()), exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(method, path, str(exc)) from exc
        if status < 200 or status >= 300:
            raise ApiError(method, path, _response_detail(raw), status)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError(method, path, "response không phải UTF-8") from exc


def _response_detail(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")[:300] or "response rỗng"
    if isinstance(value, dict) and "detail" in value:
        return str(value["detail"])
    return json.dumps(value, ensure_ascii=False)[:300]


def _url_segment(value: str) -> str:
    # Generated/custom session IDs are restricted, so quote defensively anyway.
    from urllib.parse import quote

    return quote(value, safe="")


def validate_session_id(value: str) -> None:
    if not value or len(value) > 63 or SESSION_ID_RE.fullmatch(value) is None:
        raise CollectionError(
            "session_id phải dài 1..63 và chỉ gồm A-Z, a-z, 0-9, '.', '_' hoặc '-'; "
            "firmware dùng buffer 64 byte"
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_session_id(prefix: str = "collect") -> str:
    value = f"{prefix}_{datetime.now(UTC):%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
    validate_session_id(value)
    return value


def _sleep_until(
    duration_s: float,
    sleep_fn: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    deadline = clock() + duration_s
    while (remaining := deadline - clock()) > 0:
        sleep_fn(min(remaining, 0.25))


def validate_collected_rows(
    rows: Iterable[Mapping[str, Any]],
    session: Mapping[str, Any],
    spec: SessionSpec,
    *,
    timing_config: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Validate complete boundaries before assigning any dataset labels."""
    validator = Draft202012Validator(
        json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    )
    raw_rows = [dict(row) for row in rows]
    for index, row in enumerate(raw_rows):
        try:
            validator.validate(row)
        except ValidationError as exc:
            raise CollectionError(f"row {index}: schema {exc.message}") from exc
        if row["boot_id"].startswith(("synthetic_", "sim_")):
            raise CollectionError("boot simulator/synthetic: không gắn synthetic=false")

    if session.get("status") != "closed":
        raise CollectionError("chỉ xuất session đã đóng")
    if session.get("scenario") != spec.scenario or session.get("object") != spec.target_type:
        raise CollectionError("nhãn iot_backend lệch scenario/target_type của lượt")
    start_sequence = _optional_int(session.get("start_sequence"))
    stop_sequence = _optional_int(session.get("stop_sequence"))
    start_boot_id = session.get("start_boot_id")
    if start_sequence is None or stop_sequence is None or start_boot_id is None:
        raise CollectionError("boundary boot/sequence chưa đầy đủ")

    issues: list[str] = []
    if session.get("boot_change"):
        issues.append("boot_change=1 trong session")
    if session.get("boundary_source") != "device_ack":
        issues.append("START dùng db_fallback, chưa xác nhận tín hiệu tại ESP32")
    seen: set[tuple[str, str, int]] = set()
    for index, row in enumerate(raw_rows):
        key = (row["device_id"], row["boot_id"], row["sequence"])
        if key in seen:
            raise CollectionError(f"trùng sample key ở row {index}: {key}")
        seen.add(key)
        if row["device_id"] != session["device_id"] or row["boot_id"] != start_boot_id:
            raise CollectionError(f"row {index}: device/boot lệch boundary")
        if not start_sequence <= row["sequence"] <= stop_sequence:
            raise CollectionError(f"row {index}: sequence ngoài boundary START/STOP")

    ordered = sorted(raw_rows, key=lambda row: row["sequence"])
    expected = max(0, stop_sequence - start_sequence + 1)
    missing_sequences = expected - len(ordered)
    if missing_sequences:
        issues.append(f"thiếu {missing_sequences} sequence trong boundary")
    if not ordered:
        issues.append("session không có mẫu")
    intervals = [
        (second["monotonic_us"] - first["monotonic_us"]) / 1_000_000
        for first, second in zip(ordered, ordered[1:])
    ]
    config = timing_config or yaml.safe_load(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    delta_s = float(config["delta_s"])
    jitter_s = float(config["max_time_jitter_s"])
    max_gap_s = float(config["max_gap_s"])
    timing_errors = sum(abs(dt - delta_s) > jitter_s or dt > max_gap_s for dt in intervals)
    if timing_errors:
        issues.append(f"{timing_errors} khoảng mẫu lệch nhịp/gap")
    invalid_count = sum(row["valid"] is not True for row in ordered)
    if invalid_count:
        issues.append(f"có {invalid_count} mẫu invalid (giữ để windowing xử lý)")
    distances = [row["distance_mm"] for row in ordered if row["valid"]]
    quality = {
        "rows": len(ordered),
        "expected_rows": expected,
        "invalid_count": invalid_count,
        "missing_sequences": missing_sequences,
        "timing_error_count": timing_errors,
        "distance_min_mm": min(distances) if distances else None,
        "distance_max_mm": max(distances) if distances else None,
        "interval_median_s": _median(intervals),
        "interval_min_s": min(intervals) if intervals else None,
        "interval_max_s": max(intervals) if intervals else None,
        "timing_config": {"delta_s": delta_s, "max_time_jitter_s": jitter_s, "max_gap_s": max_gap_s},
        # Current API exposes only START ACK/fallback provenance.
        "stop_boundary_source": "not_exposed_by_api",
    }
    labelled = [
        {
            **row,
            "session_label": str(session["session_id"]),
            "synthetic": False,
            "scenario": spec.scenario,
            "target_type": spec.target_type,
            "speed_level": spec.speed_level,
        }
        for row in ordered
    ]
    return labelled, quality, sorted(set(issues))


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


class CollectionRunner:
    """Own one collection output and record each request before sending it."""

    def __init__(
        self,
        api: SessionApi,
        output_dir: str | Path,
        device_id: str,
        *,
        settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S,
        poll_s: float = DEFAULT_POLL_S,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        run_id: str | None = None,
        allow_db_fallback: bool = False,
        timing_config: Mapping[str, Any] | None = None,
    ):
        if not all(math.isfinite(n) and n > 0 for n in (settle_timeout_s, poll_s)):
            raise CollectionError("settle_timeout_s và poll_s phải hữu hạn và > 0")
        self.api = api
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if any(self.output_dir.iterdir()):
            raise CollectionError(f"thư mục {self.output_dir} đã có dữ liệu; chọn thư mục mới")
        self.device_id = device_id
        self.settle_timeout_s = settle_timeout_s
        self.poll_s = poll_s
        self.sleep_fn = sleep_fn
        self.clock = clock
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.run_id = run_id or _new_session_id("dataset")
        self.allow_db_fallback = allow_db_fallback
        self.timing_config = dict(timing_config or yaml.safe_load(MODEL_CONFIG_PATH.read_text(encoding="utf-8")))
        self.created_at = _utc_now()
        self.results: list[CollectedSession] = []
        self.attempts: list[dict[str, Any]] = []
        self.manifest_path = self.output_dir / "manifest.json"
        self.combined_path = self.output_dir / "dataset.jsonl"
        self._write_manifest()

    def collect_one(
        self,
        spec: SessionSpec,
        *,
        prompt: bool = True,
        non_interactive: bool = False,
    ) -> CollectedSession:
        spec.validate()
        if (non_interactive or not prompt) and spec.duration_s is None:
            raise CollectionError("non-interactive cần duration_s để biết lúc gửi STOP")
        session_id = spec.session_id or _new_session_id("run")
        self.output_fn(
            f"\nKịch bản: {spec.scenario} | mục tiêu: {spec.target_type} | "
            f"tốc độ: {spec.speed_level or 'n/a'} | session: {session_id}"
        )
        if spec.zone_note:
            self.output_fn(f"Ghi chú: {spec.zone_note}")
        if prompt and not non_interactive:
            self.input_fn("Chuẩn bị mục tiêu, Enter để gửi START (Ctrl+C để hủy)... ")
        health = self.api.health()
        if health.get("status") != "ok" or health.get("mqtt") is not True:
            raise CollectionError("iot_backend/MQTT chưa sẵn sàng")
        existing = self.api.list_sessions()
        if any(s["session_id"] == session_id for s in existing) or any(
            a["session_id"] == session_id for a in self.attempts
        ):
            raise CollectionError(f"session_id {session_id} đã được sử dụng; chọn ID mới")
        running = [s for s in existing if s.get("status") == "running" and s.get("device_id") == self.device_id]
        if running:
            raise CollectionError(f"session {running[0]['session_id']} đang chạy; không mở lượt mới")

        attempt = {"session_id": session_id, "spec": asdict(spec), "state": "start_requested", "updated_at": _utc_now()}
        self.attempts.append(attempt)
        self._write_manifest()
        start_attempted = False
        owns_session = False
        stop_attempted = False
        interrupted = False

        def send_stop() -> dict[str, Any]:
            nonlocal stop_attempted
            self._update_attempt(attempt, "stop_requested")
            stop_attempted = True
            response = self.api.stop_session(session_id)
            if response.get("session_id") != session_id or response.get("status") != "closed":
                raise CollectionError("STOP chưa xác nhận session đã đóng")
            self._update_attempt(attempt, "closed", stop_response=response)
            return response

        try:
            # Never replay a POST when its response is lost. The request journal
            # keeps the ID needed to reconcile through read-only API calls.
            start_attempted = True
            start_info = self.api.start_session({
                "session_id": session_id,
                "scenario": spec.scenario,
                "object": spec.target_type,
                "zone_note": spec.zone_note,
            })
            if start_info.get("session_id") != session_id or start_info.get("status") != "running":
                raise CollectionError("START chưa xác nhận đúng session đang chạy")
            owns_session = True
            self._update_attempt(attempt, "running", start_response=start_info)
            if start_info.get("device_id") != self.device_id:
                raise CollectionError("device_id iot_backend lệch --device-id; thu bị hủy")
            if start_info.get("scenario") != spec.scenario or start_info.get("object") != spec.target_type:
                raise CollectionError("metadata START lệch kịch bản đã chọn")
            if start_info.get("boundary_source") != "device_ack" and not self.allow_db_fallback:
                raise CollectionError(
                    "START chưa nhận ACK ESP32 trong thời gian chờ; đã hủy lượt. "
                    "Kiểm tra MQTT/outbox và kết nối board"
                )
            if start_info.get("start_boot_id") is None or start_info.get("start_sequence") is None:
                raise CollectionError("START chưa có boot/sequence để gán nhãn")
            self.output_fn(
                f"START: {start_info.get('boundary_source')}, sequence={start_info.get('start_sequence')}. "
                "Bắt đầu thực hiện kịch bản."
            )
            try:
                if spec.duration_s is not None:
                    self.output_fn(f"Đang thu {spec.duration_s:g} giây; sau đó tự gửi STOP.")
                    _sleep_until(spec.duration_s, self.sleep_fn, self.clock)
                else:
                    self.input_fn("Thực hiện xong, Enter để gửi STOP... ")
            except (KeyboardInterrupt, EOFError):
                interrupted = True
                self.output_fn("Đang đóng và lưu lượt bị ngắt...")

            stop_info = send_stop()
            self.output_fn(f"Session đã đóng: stop_sequence={stop_info.get('stop_sequence')}")
            final_info, rows = self._wait_for_export(session_id)
            # Save unlabelled evidence before validating schema/provenance.
            _write_jsonl(self.output_dir / f"{session_id}.raw.jsonl", rows)
            if final_info.get("session_id") != session_id or final_info.get("device_id") != self.device_id:
                raise CollectionError("export không thuộc session/device runner đang sở hữu")
            labelled, quality, issues = validate_collected_rows(rows, final_info, spec, timing_config=self.timing_config)
            if interrupted:
                issues.append("lượt bị ngắt bởi người vận hành")
            output_file = self.output_dir / f"{session_id}.jsonl"
            _write_jsonl(output_file, labelled)
            _append_jsonl(self.combined_path, labelled)
            result = CollectedSession(
                session_id=session_id, scenario=spec.scenario, target_type=spec.target_type,
                speed_level=spec.speed_level, output_file=str(output_file), rows=len(labelled),
                quality_ok=not issues, quality_issues=issues,
                boundary={
                    "start_sequence": final_info.get("start_sequence"),
                    "stop_sequence": final_info.get("stop_sequence"),
                    "start_boot_id": final_info.get("start_boot_id"),
                    "boundary_source": final_info.get("boundary_source"),
                    "stop_boundary_source": "not_exposed_by_api",
                    "boot_change": final_info.get("boot_change"),
                },
                quality=quality, dataset_sha256=hashlib.sha256(output_file.read_bytes()).hexdigest(),
            )
            self.results.append(result)
            self._update_attempt(attempt, "interrupted" if interrupted else "exported")
            self.output_fn(f"Đã lưu {result.rows} mẫu, quality={'PASS' if result.quality_ok else 'WARN'}: {output_file}")
            if issues:
                self.output_fn("; ".join(issues))
        except (KeyboardInterrupt, EOFError, CollectionError, OSError, ValueError) as exc:
            # An API timeout after START/STOP remains ambiguous. Do not repeat
            # that request, stop somebody else's session, or hide its journal.
            if owns_session and not stop_attempted:
                try:
                    send_stop()
                except (CollectionError, OSError, ValueError) as stop_error:
                    attempt["cleanup_error"] = str(stop_error)
            unresolved = (start_attempted and not owns_session) or (stop_attempted and attempt["state"] == "stop_requested")
            self._update_attempt(attempt, "needs_reconciliation" if unresolved else "failed", error=str(exc) or type(exc).__name__)
            self.output_fn(f"Lượt {session_id}: {exc}. Trạng thái lưu tại {self.manifest_path}")
            if (isinstance(exc, CollectionError) and not unresolved
                    and attempt.get("stop_response", {}).get("status") == "closed"):
                raise ClosedRunError(str(exc)) from exc
            raise
        if interrupted:
            raise KeyboardInterrupt
        return result

    def _wait_for_export(
        self, session_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Wait for every expected sequence, including samples behind STOP ACK."""
        deadline = self.clock() + self.settle_timeout_s
        while True:
            info = self.api.get_session(session_id)
            rows = self.api.export_session(session_id)
            start, stop = info.get("start_sequence"), info.get("stop_sequence")
            complete = False
            if start is not None and stop is not None:
                expected = max(0, int(stop) - int(start) + 1)
                sequences = {
                    row["sequence"] for row in rows
                    if isinstance(row.get("sequence"), int)
                    and row.get("boot_id") == info.get("start_boot_id")
                    and row.get("device_id") == info.get("device_id")
                    and int(start) <= row["sequence"] <= int(stop)
                }
                complete = len(sequences) == expected
            if complete or self.clock() >= deadline:
                return info, rows
            self.sleep_fn(min(self.poll_s, max(0, deadline - self.clock())))

    def _update_attempt(self, attempt: dict[str, Any], state: str, **details: Any) -> None:
        attempt.update(state=state, updated_at=_utc_now(), **details)
        self._write_manifest()

    def _write_manifest(self) -> None:
        data = {
            "schema_version": "collection-run-v1", "run_id": self.run_id,
            "created_at": self.created_at, "updated_at": _utc_now(), "source": "real",
            "device_id": self.device_id, "api_url": getattr(self.api, "base_url", None),
            "sessions": [result.to_dict() for result in self.results], "attempts": self.attempts,
            "rows": sum(result.rows for result in self.results),
            "quality_pass": sum(result.quality_ok for result in self.results),
            "quality_warn": sum(not result.quality_ok for result in self.results),
            "dataset_sha256": hashlib.sha256(self.combined_path.read_bytes()).hexdigest() if self.combined_path.exists() else None,
        }
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.manifest_path)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(dict(row), ensure_ascii=False, allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, allow_nan=False) + "\n")


def _coerce_optional_string(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _spec_from_mapping(item: Mapping[str, Any], defaults: Mapping[str, Any]) -> SessionSpec:
    merged = {**defaults, **item}
    # ``object`` is an accepted alias for ``target_type``.  An explicit
    # per-entry alias must override a default target_type.
    if "target_type" in item:
        target = item["target_type"]
    elif "object" in item:
        target = item["object"]
    else:
        target = defaults.get("target_type", defaults.get("object"))
    if not isinstance(target, str) or not target.strip():
        raise CollectionError("mỗi lượt cần target_type hoặc object là chuỗi không rỗng")
    scenario = merged.get("scenario")
    if not isinstance(scenario, str) or not scenario.strip():
        raise CollectionError("mỗi lượt cần scenario là chuỗi không rỗng")
    duration = merged.get("duration_s")
    spec = SessionSpec(
        scenario=scenario.strip(),
        target_type=str(target).strip(),
        speed_level=_coerce_optional_string(merged.get("speed_level")),
        duration_s=None if duration in (None, "") else float(duration),
        zone_note=_coerce_optional_string(merged.get("zone_note")),
        session_id=_coerce_optional_string(merged.get("session_id")),
    )
    spec.validate()
    return spec


def load_plan(path: str | Path) -> list[SessionSpec]:
    """Load YAML plan and expand optional ``repetitions``."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise CollectionError("plan YAML phải là object")
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, Mapping):
        raise CollectionError("plan.defaults phải là object")
    entries = raw.get("sessions", raw.get("runs"))
    if not isinstance(entries, list) or not entries:
        raise CollectionError("plan cần sessions: [ ... ] không rỗng")
    specs: list[SessionSpec] = []
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping):
            raise CollectionError(f"plan.sessions[{index}] phải là object")
        repetitions = int(item.get("repetitions", 1))
        if repetitions < 1:
            raise CollectionError(f"plan.sessions[{index}].repetitions phải >= 1")
        base = dict(item)
        base.pop("repetitions", None)
        for repetition in range(1, repetitions + 1):
            expanded = dict(base)
            if repetitions > 1:
                base_id = expanded.get("session_id")
                expanded["session_id"] = (
                    f"{base_id}_{repetition:03d}" if base_id else None
                )
            specs.append(_spec_from_mapping(expanded, defaults))
    ids = [spec.session_id for spec in specs if spec.session_id is not None]
    if len(ids) != len(set(ids)):
        raise CollectionError("plan có session_id trùng sau khi mở rộng repetitions")
    return specs


def prompt_spec() -> SessionSpec | None:
    """Ask for one run; blank scenario exits the interactive collector."""
    print("\nScenario: stationary | approach | recede | stop | reversal | oscillation | sensor_check")
    scenario = input("Chọn scenario (Enter để thoát): ").strip()
    if not scenario:
        return None
    target = input("Target type/object (ví dụ person, flat_board): ").strip()
    speed = input("Speed level (slow/medium/fast, có thể bỏ trống): ").strip() or None
    duration_raw = input("Thời lượng giây (Enter để tự nhấn STOP): ").strip()
    duration = float(duration_raw) if duration_raw else None
    note = input("Ghi chú thao tác (có thể bỏ trống): ").strip() or None
    return SessionSpec(scenario, target, speed, duration, note)


def _interactive_loop(runner: CollectionRunner) -> int:
    while True:
        try:
            spec = prompt_spec()
            if spec is None:
                return 0
            runner.collect_one(spec)
        except KeyboardInterrupt:
            runner.output_fn("Đã hủy lượt thu.")
            return 130
        except ClosedRunError as exc:
            runner.output_fn(f"LỖI lượt thu: {exc}")
            runner.output_fn("Session lỗi đã đóng; có thể chọn lượt mới. Lượt lỗi giữ trong manifest.")
        except (CollectionError, ValueError) as exc:
            runner.output_fn(f"LỖI lượt thu: {exc}")
            # An unresolved boundary must be reconciled before another run.
            return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Thu dataset thật: mỗi lượt gửi START/STOP xuống ESP32 và gắn nhãn session."
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--plan", help="YAML plan; bỏ qua để dùng interactive mode")
    parser.add_argument("--scenario")
    parser.add_argument("--target-type", "--object", dest="target_type")
    parser.add_argument("--speed-level")
    parser.add_argument("--duration-s", type=float)
    parser.add_argument("--zone-note")
    parser.add_argument("--session-id")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--allow-db-fallback", action="store_true", help="Cho phép START chưa có ACK; lượt sẽ được đánh dấu WARN")
    parser.add_argument("--settle-timeout-s", type=float, default=DEFAULT_SETTLE_TIMEOUT_S)
    parser.add_argument("--poll-s", type=float, default=DEFAULT_POLL_S)
    return parser


def _single_spec_from_args(args: argparse.Namespace) -> SessionSpec | None:
    supplied = any(
        value is not None
        for value in (args.scenario, args.target_type, args.speed_level, args.duration_s, args.zone_note, args.session_id)
    )
    if not supplied:
        return None
    if not args.scenario or not args.target_type:
        raise CollectionError("dùng command-line mode cần cả --scenario và --target-type")
    return SessionSpec(
        scenario=args.scenario,
        target_type=args.target_type,
        speed_level=args.speed_level,
        duration_s=args.duration_s,
        zone_note=args.zone_note,
        session_id=args.session_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not all(math.isfinite(n) and n > 0 for n in (args.settle_timeout_s, args.poll_s)):
        parser.error("--settle-timeout-s và --poll-s phải > 0")
    if args.plan and any(
        value is not None
        for value in (args.scenario, args.target_type, args.speed_level, args.duration_s, args.zone_note, args.session_id)
    ):
        parser.error("--plan không dùng chung với --scenario/--target-type/... ")
    try:
        specs = load_plan(args.plan) if args.plan else None
        single = None if specs else _single_spec_from_args(args)
        if args.non_interactive and specs:
            for spec in specs:
                if spec.duration_s is None:
                    raise CollectionError("non-interactive với --plan yêu cầu duration_s cho mọi lượt")
        if single is not None:
            single.validate()
            if args.non_interactive and single.duration_s is None:
                raise CollectionError("non-interactive cần --duration-s")
        if args.non_interactive and specs is None and single is None:
            raise CollectionError("non-interactive cần --plan hoặc --scenario")
        if args.output_dir:
            output_dir = Path(args.output_dir)
        else:
            output_dir = Path("data/datasets") / _new_session_id("collection")
        api = HttpSessionApi(args.api_url)
        runner = CollectionRunner(
            api,
            output_dir,
            args.device_id,
            settle_timeout_s=args.settle_timeout_s,
            poll_s=args.poll_s,
            allow_db_fallback=args.allow_db_fallback,
        )
        if specs is not None:
            for spec in specs:
                runner.collect_one(spec, prompt=not args.non_interactive, non_interactive=args.non_interactive)
            return 0
        if single is not None:
            runner.collect_one(single, prompt=not args.non_interactive, non_interactive=args.non_interactive)
            return 0
        print(f"Dataset sẽ lưu tại: {output_dir}")
        return _interactive_loop(runner)
    except KeyboardInterrupt:
        print("Đã hủy collector.", file=sys.stderr)
        return 130
    except (CollectionError, OSError, ValueError, EOFError, yaml.YAMLError) as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
