"""CLI ingest telemetry từ file JSONL vào SQLite qua ``IngestionService``.

Chạy từ repo root::

    .venv/bin/python -m scripts.ingest_jsonl --input data/samples/demo.jsonl

Mỗi dòng là một payload telemetry. Dòng trắng bỏ qua; dòng hỏng (JSON sai,
không phải object, thiếu device_id) được đếm ``invalid_lines`` và ghi vào
bảng rejected_messages qua service với payload thô. Exit 0 kể cả khi có
rejected — dữ liệu xấu được ghi nhận đúng cách không phải lỗi chương trình.

Mẫu nạp được gán vào session đang chạy nếu có (ranh giới start theo db_fallback).
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from backend.ingestion.service import IngestionService
from backend.ingestion.store import Store
from backend.inference.predictor import SafetyPredictor
from backend.safety import load_safety_configs
from backend.sessions.manager import SessionManager
from backend.settings import load_settings
from dotenv import load_dotenv

__all__ = ["main"]

logger = logging.getLogger(__name__)

_FALLBACK_TOPIC = "hrc/v1/devices/unknown/telemetry"


def _make_predictor(model_dir: str, model_config_path: str) -> SafetyPredictor | None:
    """SafetyPredictor instance RIÊNG cho lần chạy CLI này (M6b).

    Instance mới mỗi lần chạy: buffer cửa sổ trượt chỉ sống trong tiến trình
    ingest, KHÔNG chia sẻ state với backend live. Thiếu model.joblib hoặc
    manifest.json → None (prediction MODEL_UNAVAILABLE như trước M6b).
    Mode 'live' của handle_message được service truyền xuống predictor — cờ
    stale so tuổi mẫu với stale_after_s của config.
    """
    directory = Path(model_dir)
    if not (
        (directory / "model.joblib").is_file()
        and (directory / "manifest.json").is_file()
    ):
        logger.info(
            "Không có model artifact trong %s — ingest không kèm dự báo "
            "(prediction MODEL_UNAVAILABLE)",
            directory,
        )
        return None
    with Path(model_config_path).open(encoding="utf-8") as handle:
        model_config = yaml.safe_load(handle)
    return SafetyPredictor(directory, model_config)


def main(argv: list[str] | None = None) -> int:
    """Đọc JSONL, ingest từng dòng, in summary JSON và luôn exit 0."""
    parser = argparse.ArgumentParser(
        description="Ingest telemetry JSONL vào HRC Safety Log DB."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="File JSONL, mỗi dòng một payload telemetry",
    )
    parser.add_argument(
        "--db", default=None, help="Đường dẫn SQLite (mặc định từ settings)"
    )
    parser.add_argument(
        "--safety-config",
        default=None,
        help="File YAML cấu hình safety (mặc định từ settings)",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help=(
            "Thư mục artifact model (model.joblib + manifest.json); mặc định "
            "lấy settings.model_artifact_dir (env MODEL_ARTIFACT_DIR). "
            "Thiếu artifact → ingest không kèm dự báo (MODEL_UNAVAILABLE)."
        ),
    )
    parser.add_argument(
        "--env-file", default=None, help="File .env tùy chọn để nạp biến môi trường"
    )
    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file)
    elif Path(".env").exists():
        # M3c m-4: tự nạp .env ở repo root khi không chỉ định --env-file
        # (cùng nhánh với scripts/replay.py và scripts/session_cli.py).
        load_dotenv(Path(".env"))
    settings = load_settings()
    db_path = args.db if args.db is not None else settings.db_path
    safety_config = (
        args.safety_config if args.safety_config is not None else settings.safety_config_path
    )

    counts = {"stored": 0, "duplicate": 0, "rejected": 0, "invalid_lines": 0}
    store = Store(db_path)
    try:
        manager = SessionManager(store, settings.device_id, control=None)
        model_dir = (
            args.model_dir if args.model_dir is not None else settings.model_artifact_dir
        )
        predictor = _make_predictor(model_dir, settings.model_config_path)
        service = IngestionService(
            store,
            load_safety_configs(safety_config),
            session_assigner=manager,
            predictor=predictor,
        )
        with open(args.input, encoding="utf-8") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload: Any = json.loads(line)
                except ValueError:
                    payload = None
                topic = _topic_for(payload)
                if topic is None:
                    # Không xác định được device_id: ghi rejected với payload thô.
                    counts["invalid_lines"] += 1
                    logger.warning("Dòng %d không hợp lệ, ghi rejected", line_no)
                    service.handle_message(_FALLBACK_TOPIC, line)
                    continue
                summary = service.handle_message(topic, line)
                counts[summary["action"]] += 1
        # M3c m-5: tính nốt raw pending của lần chạy trước (mẫu duplicate
        # đã nằm trong raw nhưng chưa kịp compute) — như catch_up khi restart.
        service.catch_up()
    finally:
        store.close()

    print(json.dumps(counts))
    return 0


def _topic_for(payload: Any) -> str | None:
    """Topic telemetry từ device_id; None nếu payload không phải dict có device_id."""
    if isinstance(payload, dict):
        device_id = payload.get("device_id")
        if isinstance(device_id, str) and device_id:
            return f"hrc/v1/devices/{device_id}/telemetry"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
