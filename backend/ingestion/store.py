"""Lưu trữ SQLite WAL: raw_samples, rejected_messages, processed_samples.

DDL duy nhất nằm ở ``backend/ingestion/schema.sql``; mọi truy cập DB đi qua
``Store`` (thread-safe cho demo: một kết nối chung + RLock cho thread paho
MQTT và thread pool FastAPI).
"""

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.safety import STATUS_CLEAR, STATUS_UNKNOWN, STATUS_VIOLATION

__all__ = ["RawInsertResult", "Store"]

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


@dataclass(frozen=True)
class RawInsertResult:
    """Kết quả chèn một raw sample."""

    inserted: bool
    is_late: bool
    reason: str | None  # 'duplicate' khi inserted=False


class Store:
    """SQLite WAL, thread-safe cho demo (paho thread + FastAPI)."""

    def __init__(self, db_path: str | Path):
        """Mở kết nối, bật PRAGMA WAL/busy_timeout/foreign_keys và tạo schema."""
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: chế độ autocommit, transaction quản lý thủ công.
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ raw

    def insert_raw(self, payload: dict, received_at: str) -> RawInsertResult:
        """Chèn một raw sample; trùng khóa (device_id, boot_id, sequence) thì bỏ.

        ``is_late``: sequence nhỏ hơn MAX(sequence) đã nhận của cùng
        (device_id, boot_id) — tính TRƯỚC khi insert. Toàn bộ đọc MAX + insert
        chạy trong một transaction (BEGIN IMMEDIATE) cùng lock.
        """
        device_id = payload["device_id"]
        boot_id = payload["boot_id"]
        sequence = int(payload["sequence"])
        payload_json = json.dumps(payload, sort_keys=True)
        time_synced = payload.get("time_synced")
        valid = payload.get("valid")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT MAX(sequence) AS max_seq FROM raw_samples "
                    "WHERE device_id=? AND boot_id=?",
                    (device_id, boot_id),
                ).fetchone()
                max_seq = row["max_seq"] if row is not None else None
                is_late = max_seq is not None and sequence < max_seq
                self._conn.execute(
                    """
                    INSERT INTO raw_samples (
                        device_id, boot_id, sequence, received_at, is_late,
                        payload_json, measured_at, monotonic_us, time_synced,
                        distance_mm, echo_us, valid, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id,
                        boot_id,
                        sequence,
                        received_at,
                        int(is_late),
                        payload_json,
                        payload.get("measured_at"),
                        payload.get("monotonic_us"),
                        None if time_synced is None else int(bool(time_synced)),
                        payload.get("distance_mm"),
                        payload.get("echo_us"),
                        None if valid is None else int(bool(valid)),
                        payload.get("error_code"),
                    ),
                )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                self._conn.execute("ROLLBACK")
                return RawInsertResult(
                    inserted=False, is_late=is_late, reason="duplicate"
                )
            return RawInsertResult(inserted=True, is_late=is_late, reason=None)

    def max_sequence(self, device_id: str, boot_id: str) -> int | None:
        """MAX(sequence) đã nhận của một (device_id, boot_id); None nếu chưa có."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(sequence) AS max_seq FROM raw_samples "
                "WHERE device_id=? AND boot_id=?",
                (device_id, boot_id),
            ).fetchone()
            return None if row is None else row["max_seq"]

    def gaps(self, device_id: str, boot_id: str) -> list[tuple[int, int]]:
        """Các khoảng sequence thiếu giữa min và max đã nhận của một boot.

        Ví dụ: [1,2,5,6] → [(3,4)]; [1,3,5] → [(2,2),(4,4)].
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT sequence FROM raw_samples WHERE device_id=? AND boot_id=? "
                "ORDER BY sequence",
                (device_id, boot_id),
            ).fetchall()
        sequences = [row["sequence"] for row in rows]
        missing: list[tuple[int, int]] = []
        for prev, nxt in zip(sequences, sequences[1:]):
            if nxt > prev + 1:
                missing.append((prev + 1, nxt - 1))
        return missing

    def fetch_raw(self, device_id: str, boot_id: str) -> list[sqlite3.Row]:
        """Mọi raw sample của một (device_id, boot_id), sắp theo sequence."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM raw_samples WHERE device_id=? AND boot_id=? "
                "ORDER BY sequence",
                (device_id, boot_id),
            ).fetchall()

    def pending_raw(self, processor_version: str) -> list[sqlite3.Row]:
        """Raw chưa có processed_samples cho ``processor_version`` đã cho.

        LEFT JOIN cùng khóa + processor_version, ORDER BY device_id, boot_id,
        sequence — dùng cho restart recovery (catch_up).
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT r.* FROM raw_samples r
                LEFT JOIN processed_samples p
                  ON p.device_id = r.device_id
                 AND p.boot_id = r.boot_id
                 AND p.sequence = r.sequence
                 AND p.processor_version = ?
                WHERE p.id IS NULL
                ORDER BY r.device_id, r.boot_id, r.sequence
                """,
                (processor_version,),
            ).fetchall()

    # -------------------------------------------------------------- rejected

    def mark_rejected(
        self,
        topic: str | None,
        reason: str,
        payload_text: str | None,
        received_at: str,
    ) -> None:
        """Ghi nhận một message bị từ chối (JSON hỏng, topic sai, schema sai...)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejected_messages "
                "(received_at, topic, reason, payload_text) VALUES (?, ?, ?, ?)",
                (received_at, topic, reason, payload_text),
            )

    # ------------------------------------------------------------- processed

    def insert_processed(
        self,
        *,
        device_id: str,
        boot_id: str,
        sequence: int,
        processor_version: str,
        computed_at: str,
        mode: str,
        result_json: str,
    ) -> bool:
        """INSERT OR IGNORE; True nếu insert mới, False nếu đã có (idempotent)."""
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO processed_samples (
                    device_id, boot_id, sequence, processor_version,
                    computed_at, mode, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    boot_id,
                    sequence,
                    processor_version,
                    computed_at,
                    mode,
                    result_json,
                ),
            )
            return cur.rowcount > 0

    def fetch_processed(
        self, device_id: str, boot_id: str, processor_version: str
    ) -> list[sqlite3.Row]:
        """Processed samples của một khóa, sắp theo sequence."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM processed_samples WHERE device_id=? AND boot_id=? "
                "AND processor_version=? ORDER BY sequence",
                (device_id, boot_id, processor_version),
            ).fetchall()

    # ------------------------------------------------------------ sessions

    def insert_session(
        self,
        session_id: str,
        device_id: str,
        scenario: str | None,
        obj: str | None,
        zone_note: str | None,
        started_at: str,
        config_json: str | None,
        start_boot_id: str | None,
        start_sequence: int | None,
        boundary_source: str,
    ) -> None:
        """Tạo một session mới với trạng thái 'running' và ranh giới start.

        ``session_id`` trùng PK (đã tồn tại) → ``ValueError`` (API map 409).
        """
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, device_id, scenario, object, zone_note,
                        status, started_at, config_json, start_boot_id,
                        start_sequence, boundary_source
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        device_id,
                        scenario,
                        obj,
                        zone_note,
                        started_at,
                        config_json,
                        start_boot_id,
                        start_sequence,
                        boundary_source,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"session_id {session_id} đã tồn tại") from exc

    def close_session(self, session_id: str, ended_at: str, stop_sequence: int | None) -> None:
        """Đóng session: status='closed', ghi ended_at và stop_sequence."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='closed', ended_at=?, stop_sequence=? "
                "WHERE session_id=?",
                (ended_at, stop_sequence, session_id),
            )

    def set_boot_change(self, session_id: str) -> None:
        """Đánh dấu session đã thấy mẫu từ boot khác boot start (boot_change=1)."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET boot_change=1 WHERE session_id=?", (session_id,)
            )

    def pin_session_boot(self, session_id: str, boot_id: str) -> bool:
        """Pin ``start_boot_id`` cho session running còn NULL (M3c m-3).

        Session bắt đầu trên DB rỗng có ``start_boot_id=NULL`` — nếu không pin,
        ``session_for_sample`` sẽ nhận mẫu từ MỌI boot mà không bao giờ flag
        ``boot_change``. Cập nhật có điều kiện ``WHERE start_boot_id IS NULL``
        nên chỉ chạy một lần (writer đầu tiên thắng — mẫu đầu tiên gán được).
        Chỉ đụng session đang running; session đã đóng giữ nguyên ranh giới.
        Trả True nếu pin thành công.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET start_boot_id=? "
                "WHERE session_id=? AND start_boot_id IS NULL AND status='running'",
                (boot_id, session_id),
            )
            return cur.rowcount > 0

    def running_session(self, device_id: str) -> sqlite3.Row | None:
        """Session 'running' của một device (mới nhất theo started_at); None nếu không có."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sessions WHERE device_id=? AND status='running' "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (device_id,),
            ).fetchone()

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        """Lấy một session theo session_id; None nếu không có."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()

    def list_sessions(self) -> list[sqlite3.Row]:
        """Mọi session, sắp theo started_at tăng dần."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at, rowid"
            ).fetchall()

    def session_for_sample(
        self, device_id: str, boot_id: str, sequence: int
    ) -> str | None:
        """Session gán được cho một mẫu theo khoảng sequence cùng boot.

        Điều kiện: device khớp, ``start_boot_id`` = boot (hoặc NULL — session
        bắt đầu khi DB rỗng, chấp nhận boot bất kỳ), ``start_sequence`` <=
        sequence (hoặc NULL) và ``stop_sequence`` IS NULL hoặc sequence <=
        stop_sequence. Ưu tiên session có start_sequence lớn nhất.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT session_id FROM sessions
                WHERE device_id=?
                  AND (start_boot_id IS NULL OR start_boot_id=?)
                  AND (start_sequence IS NULL OR start_sequence<=?)
                  AND (stop_sequence IS NULL OR ?<=stop_sequence)
                ORDER BY start_sequence IS NULL, start_sequence DESC, rowid DESC
                LIMIT 1
                """,
                (device_id, boot_id, sequence, sequence),
            ).fetchone()
        return None if row is None else row["session_id"]

    def assign_session_sample(
        self, session_id: str, device_id: str, boot_id: str, sequence: int
    ) -> bool:
        """Gán mẫu vào session — INSERT OR IGNORE (idempotent). True nếu insert mới."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO session_samples "
                "(session_id, device_id, boot_id, sequence) VALUES (?, ?, ?, ?)",
                (session_id, device_id, boot_id, sequence),
            )
            return cur.rowcount > 0

    def session_max_sequence(self, session_id: str) -> int:
        """MAX(sequence) đã gán vào session; 0 nếu chưa gán mẫu nào."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(sequence) AS max_seq FROM session_samples "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None or row["max_seq"] is None:
            return 0
        return int(row["max_seq"])

    def session_raw_rows(self, session_id: str) -> list[sqlite3.Row]:
        """Raw samples đã gán vào session, JOIN qua session_samples.

        ORDER BY device_id, boot_id, sequence — dùng cho export và replay.
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT r.* FROM session_samples s
                JOIN raw_samples r
                  ON r.device_id = s.device_id
                 AND r.boot_id = s.boot_id
                 AND r.sequence = s.sequence
                WHERE s.session_id=?
                ORDER BY r.device_id, r.boot_id, r.sequence
                """,
                (session_id,),
            ).fetchall()

    def session_stats(
        self, session_id: str, processor_version: str | None = None
    ) -> dict:
        """Thống kê một session: sample/late/gap/violation/clear/unknown counts.

        - sample_count: số mẫu đã gán.
        - late_count: mẫu gán có is_late=1 trong raw_samples.
        - gap_count: tổng độ dài các khoảng sequence thiếu giữa min-max của
          các mẫu đã gán, tính riêng từng boot.
        - violation/clear/unknown_count: đếm theo ``safety.status`` trong
          processed_samples join session_samples; chỉ đếm theo
          ``processor_version`` đã cho, ``None`` = không lọc (mọi version).
        """
        with self._lock:
            sample_rows = self._conn.execute(
                "SELECT ss.boot_id, ss.sequence, r.is_late FROM session_samples ss "
                "LEFT JOIN raw_samples r "
                  "ON r.device_id=ss.device_id AND r.boot_id=ss.boot_id "
                 "AND r.sequence=ss.sequence "
                "WHERE ss.session_id=? ORDER BY ss.boot_id, ss.sequence",
                (session_id,),
            ).fetchall()
            processed_rows = self._conn.execute(
                "SELECT p.result_json FROM session_samples ss "
                "JOIN processed_samples p "
                  "ON p.device_id=ss.device_id AND p.boot_id=ss.boot_id "
                 "AND p.sequence=ss.sequence "
                "WHERE ss.session_id=? "
                  "AND (? IS NULL OR p.processor_version=?)",
                (session_id, processor_version, processor_version),
            ).fetchall()

        late_count = sum(1 for row in sample_rows if row["is_late"])
        gap_count = 0
        prev_boot: str | None = None
        prev_seq: int | None = None
        for row in sample_rows:
            if row["boot_id"] != prev_boot:
                prev_boot, prev_seq = row["boot_id"], row["sequence"]
                continue
            gap_count += row["sequence"] - prev_seq - 1
            prev_seq = row["sequence"]

        counts = {STATUS_CLEAR: 0, STATUS_VIOLATION: 0, STATUS_UNKNOWN: 0}
        for row in processed_rows:
            try:
                record = json.loads(row["result_json"])
            except ValueError:
                continue
            status = record.get("safety", {}).get("status")
            if status in counts:
                counts[status] += 1

        return {
            "sample_count": len(sample_rows),
            "late_count": late_count,
            "gap_count": gap_count,
            "violation_count": counts[STATUS_VIOLATION],
            "clear_count": counts[STATUS_CLEAR],
            "unknown_count": counts[STATUS_UNKNOWN],
        }

    # ------------------------------------------------------------- queries

    def all_raw_rows(self) -> list[sqlite3.Row]:
        """Mọi raw sample, ORDER BY device_id, boot_id, sequence (replay không session)."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM raw_samples ORDER BY device_id, boot_id, sequence"
            ).fetchall()

    def latest_raw(self, device_id: str) -> sqlite3.Row | None:
        """Raw sample gần nhất của device theo thời điểm NHẬN; None nếu chưa có.

        Không dùng ``MAX(sequence)`` xuyên boot: device reboot → boot mới đếm
        lại từ 1, MAX toàn cục sẽ trả nhầm mẫu boot cũ. Hai bước: lấy row nhận
        gần nhất (``received_at`` DESC, tie-break ``rowid`` DESC), rồi trả về
        row có sequence lớn nhất CÙNG boot của row đó.
        """
        with self._lock:
            latest = self._conn.execute(
                "SELECT boot_id FROM raw_samples WHERE device_id=? "
                "ORDER BY received_at DESC, rowid DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            if latest is None:
                return None
            return self._conn.execute(
                "SELECT * FROM raw_samples WHERE device_id=? AND boot_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (device_id, latest["boot_id"]),
            ).fetchone()

    def processed_for_session(
        self, session_id: str, processor_version: str, limit: int
    ) -> list[sqlite3.Row]:
        """Processed samples đã gán vào session theo processor_version, giới hạn limit."""
        with self._lock:
            return self._conn.execute(
                """
                SELECT p.* FROM session_samples s
                JOIN processed_samples p
                  ON p.device_id = s.device_id
                 AND p.boot_id = s.boot_id
                 AND p.sequence = s.sequence
                WHERE s.session_id=? AND p.processor_version=?
                ORDER BY p.device_id, p.boot_id, p.sequence
                LIMIT ?
                """,
                (session_id, processor_version, limit),
            ).fetchall()

    def all_processed(self, processor_version: str, limit: int) -> list[sqlite3.Row]:
        """Processed samples của một processor_version, giới hạn limit."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM processed_samples WHERE processor_version=? "
                "ORDER BY device_id, boot_id, sequence LIMIT ?",
                (processor_version, limit),
            ).fetchall()

    def get_processed(
        self, device_id: str, boot_id: str, sequence: int, processor_version: str
    ) -> sqlite3.Row | None:
        """Một processed sample theo khóa đầy đủ; None nếu không có."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM processed_samples WHERE device_id=? AND boot_id=? "
                "AND sequence=? AND processor_version=?",
                (device_id, boot_id, sequence, processor_version),
            ).fetchone()

    # ------------------------------------------------------------------ khác

    def close(self) -> None:
        """Đóng kết nối SQLite."""
        with self._lock:
            self._conn.close()
