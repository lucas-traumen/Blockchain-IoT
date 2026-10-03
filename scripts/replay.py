"""Replay offline: tính lại safety cho raw samples với processor_version riêng.

Chạy từ repo root::

    .venv/bin/python -m scripts.replay --db data/hrc.db
    .venv/bin/python -m scripts.replay --session-id run_20260922_... 

Mặc định replay MỌI raw trong DB (``--session-id`` bỏ qua để replay toàn bộ).
Kết quả safety GIỐNG HỆT live cùng mẫu (cùng code path ``_compute_and_store``),
chỉ khác processor_version (mặc định 'replay-safety-v1'), mode='offline' và
computed_at. In summary JSON và exit 0.
"""

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path


from backend.ingestion.service import IngestionService
from backend.ingestion.store import Store
from backend.inference.predictor import SafetyPredictor, build_predictor
from backend.safety import load_safety_configs
from backend.settings import load_settings
from backend.sessions.manager import SessionManager
from dotenv import load_dotenv

__all__ = ["main"]

logger = logging.getLogger(__name__)


def _make_predictor(model_dir: str, model_config_path: str, model_type: str = "rf") -> SafetyPredictor:
    return build_predictor(model_dir, model_config_path, model_type)


def main(argv: list[str] | None = None) -> int:
    """Replay raw samples (theo session hoặc toàn bộ), in summary JSON."""
    parser = argparse.ArgumentParser(
        description="Replay offline raw samples qua thuật toán safety."
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
        "--session-id",
        default=None,
        help="Chỉ replay samples của session này (bỏ qua = replay mọi raw)",
    )
    parser.add_argument(
        "--processor-version",
        default="replay-safety-v1",
        help="Processor version ghi vào processed_samples (idempotent theo cặp này)",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help=(
            "Thư mục artifact RF/LSTM; mặc định "
            "lấy settings.model_artifact_dir (env MODEL_ARTIFACT_DIR). "
            "Thiếu artifact → replay không kèm dự báo (MODEL_UNAVAILABLE)."
        ),
    )
    parser.add_argument(
        "--env-file", default=None, help="File .env tùy chọn để nạp biến môi trường"
    )
    parser.add_argument("--model-type", choices=("rf", "lstm"), default=None)
    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file)
    elif Path(".env").exists():
        load_dotenv(Path(".env"))
    settings = load_settings()

    db_path = args.db if args.db is not None else settings.db_path
    safety_config = (
        args.safety_config if args.safety_config is not None else settings.safety_config_path
    )

    store = Store(db_path)
    try:
        model_dir = (
            args.model_dir if args.model_dir is not None else settings.model_artifact_dir
        )
        predictor = _make_predictor(
            model_dir, settings.model_config_path, args.model_type or settings.model_type
        )
        service = IngestionService(
            store, load_safety_configs(safety_config),
            session_assigner=SessionManager(store, settings.device_id, control=None),
            predictor=predictor,
        )
        if args.session_id is not None:
            if store.get_session(args.session_id) is None:
                print(f"LỖI: không tìm thấy session {args.session_id}", file=sys.stderr)
                return 1
            rows = store.session_raw_rows(args.session_id)
        else:
            rows = store.all_raw_rows()

        # compute_raw_rows trả summary từng record; đếm by_status trên các
        # record được INSERT MỚI (INSERT OR IGNORE — replay lại cùng version → 0).
        summaries = service.compute_raw_rows(
            rows, args.processor_version, mode="offline"
        )
        inserted = [s for s in summaries if s["processed_inserted"]]
        counter = Counter(s["safety_status"] for s in inserted)
        summary = {
            "processed": len(inserted),
            "by_status": {
                "CLEAR": counter.get("CLEAR", 0),
                "VIOLATION": counter.get("VIOLATION", 0),
                "UNKNOWN": counter.get("UNKNOWN", 0),
            },
            "mode": "offline",
        }
    finally:
        store.close()

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
