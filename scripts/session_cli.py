"""CLI quản lý lượt thu (session): start/stop/list/status/export.

Chạy từ repo root::

    .venv/bin/python -m scripts.session_cli start --scenario demo
    .venv/bin/python -m scripts.session_cli stop
    .venv/bin/python -m scripts.session_cli list
    .venv/bin/python -m scripts.session_cli status --session-id run_20260922_...
    .venv/bin/python -m scripts.session_cli export --session-id ... --format csv --out out.csv

Mặc định ``--no-mqtt``: ranh giới session chốt bằng fallback DB, KHÔNG cần
broker. ``--mqtt``: tạo listener ngắn để publish control và chờ ack thiết bị.
CHÚ Ý: không chạy CLI ``--mqtt`` cạnh backend đang ghi cùng một DB cùng lúc —
chỉ dùng một trong hai tại một thời điểm.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from backend.ingestion.mqtt_listener import MqttListener
from backend.ingestion.service import IngestionService
from backend.ingestion.store import Store
from backend.safety import load_safety_configs
from backend.sessions import (
    MqttControlTransport,
    SessionManager,
    export_csv,
    export_jsonl,
)
from backend.sessions.manager import snapshot_configs
from backend.settings import load_settings
from dotenv import load_dotenv

__all__ = ["main"]

logger = logging.getLogger(__name__)


def _build_manager(
    store: Store,
    device_id: str,
    safety_config: str,
    ack_timeout_s: float,
    use_mqtt: bool,
    mqtt_host: str,
    mqtt_port: int,
) -> tuple[SessionManager, MqttListener | None]:
    """Tạo SessionManager; khi ``use_mqtt`` kèm listener + control transport."""
    configs = load_safety_configs(safety_config)
    manager = SessionManager(
        store,
        device_id,
        ack_timeout_s=ack_timeout_s,
        safety_configs_snapshot=snapshot_configs(configs),
    )
    if not use_mqtt:
        return manager, None
    # CLI --mqtt: service đầy đủ để listener có thể ingest telemetry nhận được
    # trong lúc chờ ack (không cạnh backend khác đang ghi cùng DB).
    service = IngestionService(store, configs, session_assigner=manager)
    listener = MqttListener(service, mqtt_host, mqtt_port)
    control = MqttControlTransport(listener)
    manager.set_control(control)
    return manager, listener


def main(argv: list[str] | None = None) -> int:
    """Điều hướng subcommand; in JSON ra stdout, lỗi ra stderr, exit 0/1."""
    parser = argparse.ArgumentParser(
        description="Quản lý lượt thu (session) của HRC Safety Log."
    )
    parser.add_argument("--db", default=None, help="Đường dẫn SQLite (mặc định từ settings)")
    parser.add_argument(
        "--safety-config",
        default=None,
        help="File YAML cấu hình safety (mặc định từ settings)",
    )
    parser.add_argument(
        "--device-id", default=None, help="ID thiết bị (mặc định từ settings)"
    )
    parser.add_argument(
        "--env-file", default=None, help="File .env tùy chọn để nạp biến môi trường"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Bắt đầu một session mới")
    start_parser.add_argument("--scenario", default=None)
    start_parser.add_argument("--object", dest="obj", default=None)
    start_parser.add_argument("--zone-note", default=None)
    start_parser.add_argument("--session-id", default=None)
    mqtt_group = start_parser.add_mutually_exclusive_group()
    mqtt_group.add_argument("--mqtt", dest="use_mqtt", action="store_true")
    mqtt_group.add_argument("--no-mqtt", dest="use_mqtt", action="store_false")

    stop_parser = subparsers.add_parser("stop", help="Đóng session đang chạy")
    stop_parser.add_argument("--session-id", default=None)
    mqtt_group = stop_parser.add_mutually_exclusive_group()
    mqtt_group.add_argument("--mqtt", dest="use_mqtt", action="store_true")
    mqtt_group.add_argument("--no-mqtt", dest="use_mqtt", action="store_false")

    subparsers.add_parser("list", help="Liệt kê mọi session")

    status_parser = subparsers.add_parser("status", help="Chi tiết một session")
    status_parser.add_argument("--session-id", required=True)

    export_parser = subparsers.add_parser("export", help="Export raw của session")
    export_parser.add_argument("--session-id", required=True)
    export_parser.add_argument("--format", choices=["csv", "jsonl"], required=True)
    export_parser.add_argument("--out", required=True, help="File ghi kết quả")

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
    device_id = args.device_id if args.device_id is not None else settings.device_id
    use_mqtt: bool = getattr(args, "use_mqtt", False)

    store = Store(db_path)
    listener: MqttListener | None = None
    try:
        manager, listener = _build_manager(
            store,
            device_id,
            safety_config,
            settings.ack_timeout_s,
            use_mqtt,
            settings.mqtt_host,
            settings.mqtt_port,
        )
        if listener is not None:
            listener.start()
        try:
            _run_command(args, manager, store)
        finally:
            if listener is not None:
                listener.stop()
    except ValueError as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _run_command(args: argparse.Namespace, manager: SessionManager, store: Store) -> None:
    """Thực thi subcommand (đã có manager/listener sẵn sàng)."""
    if args.command == "start":
        info = manager.start_session(
            scenario=args.scenario,
            obj=args.obj,
            zone_note=args.zone_note,
            session_id=args.session_id,
        )
        print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "stop":
        info = manager.stop_session(args.session_id)
        print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "list":
        rows = [info.to_dict() for info in manager.list_sessions()]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.command == "status":
        info = manager.get_session(args.session_id)
        print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
    elif args.command == "export":
        rows = store.session_raw_rows(args.session_id)
        if args.format == "csv":
            count = export_csv(rows, args.out)
        else:
            count = export_jsonl(rows, args.out)
        print(json.dumps({"file": str(args.out), "rows": count}))


if __name__ == "__main__":
    raise SystemExit(main())
