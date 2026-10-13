"""Lưu trữ SQLite WAL: raw_samples, rejected_messages, processed_samples.

DDL duy nhất nằm ở ``iot_backend/ingestion/schema.sql``; mọi truy cập DB đi qua
``Store`` (thread-safe cho demo: một kết nối chung + RLock cho thread paho
MQTT và thread pool FastAPI).
"""

import json
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iot_backend.activity import compute_session_activity
from iot_backend.safety import STATUS_CLEAR, STATUS_UNKNOWN, STATUS_VIOLATION
from iot_backend.util import utc_now_iso

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
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, checksum) VALUES (?,?,?)",
            (1, utc_now_iso(), hashlib.sha256(_SCHEMA_PATH.read_bytes()).hexdigest()),
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self):
        """Processed assessment + event/state atomic on the shared connection."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------- reward outbox

    def get_reward_settlement(self, session_id: str) -> sqlite3.Row | None:
        """Return the durable reward bridge state for one IoT session."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM reward_settlements WHERE session_id=?", (session_id,)
            ).fetchone()

    def save_reward_settlement(
        self,
        session_id: str,
        beneficiary: str,
        outcome: str,
        evidence_hash: str | None,
        status: str,
        response: dict,
        updated_at: str,
    ) -> None:
        """Upsert reward bridge state before/after an HTTP settlement attempt."""
        if status not in {"PENDING", "SETTLED", "ERROR"}:
            raise ValueError("invalid reward settlement status")
        with self._lock:
            self._conn.execute(
                """INSERT INTO reward_settlements
                   (session_id, beneficiary, outcome, evidence_hash, status, response_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     beneficiary=excluded.beneficiary,
                     outcome=excluded.outcome,
                     evidence_hash=excluded.evidence_hash,
                     status=excluded.status,
                     response_json=excluded.response_json,
                     updated_at=excluded.updated_at""",
                (session_id, beneficiary, outcome, evidence_hash, status,
                 json.dumps(response, ensure_ascii=True, sort_keys=True), updated_at),
            )

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

    def get_raw(
        self, device_id: str, boot_id: str, sequence: int
    ) -> sqlite3.Row | None:
        """Lấy một raw sample theo khóa đầy đủ cho worker hậu xử lý."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM raw_samples WHERE device_id=? AND boot_id=? "
                "AND sequence=?",
                (device_id, boot_id, sequence),
            ).fetchone()

    def pending_raw(
        self, processor_version: str, *, session_id: str | None = None
    ) -> list[sqlite3.Row]:
        """Unprocessed raw rows, optionally bounded by one session's device/boot.

        Boundaries include committed samples that the asynchronous MQTT worker
        has not assigned to session_samples yet. Concrete range predicates let
        SQLite use the device/boot/sequence index for a closed session.
        """
        with self._lock:
            clauses = ["p.id IS NULL"]
            params: list[Any] = [processor_version]
            if session_id is not None:
                session = self._conn.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                if session is None:
                    return []
                clauses.append("r.device_id=?")
                params.append(session["device_id"])
                for column, operator in (
                    ("start_boot_id", "="),
                    ("start_sequence", ">="),
                    ("stop_sequence", "<="),
                ):
                    if session[column] is not None:
                        raw_column = "boot_id" if column == "start_boot_id" else "sequence"
                        clauses.append(f"r.{raw_column}{operator}?")
                        params.append(session[column])
            return self._conn.execute(
                "SELECT r.* FROM raw_samples r "
                "LEFT JOIN processed_samples p "
                "ON p.device_id=r.device_id AND p.boot_id=r.boot_id "
                "AND p.sequence=r.sequence AND p.processor_version=? "
                "WHERE " + " AND ".join(clauses) +
                " ORDER BY r.device_id,r.boot_id,r.sequence",
                params,
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

    def session_max_raw_sequence(self, session_id: str) -> int:
        """MAX raw sequence trong boundary, kể cả row chưa compute xong.

        MQTT commit raw trước khi worker tính assessment. Stop fallback phải
        nhìn cả raw đã bền vững để không đóng session sớm hơn dữ liệu vừa nhận.
        """
        with self._lock:
            session = self._conn.execute(
                "SELECT device_id,start_boot_id,start_sequence,started_at "
                "FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                return 0
            if session["start_boot_id"] is None:
                # Session bắt đầu trên DB rỗng: chọn boot của raw mới nhất
                # sau started_at, rồi lấy MAX cùng boot để không bị lệch bởi
                # một message đến muộn có sequence nhỏ hơn.
                latest = self._conn.execute(
                    "SELECT boot_id FROM raw_samples WHERE device_id=? "
                    "AND received_at>=? ORDER BY received_at DESC,rowid DESC LIMIT 1",
                    (session["device_id"], session["started_at"]),
                ).fetchone()
                if latest is None:
                    raw_max = 0
                else:
                    raw_row = self._conn.execute(
                        "SELECT MAX(sequence) AS max_seq FROM raw_samples "
                        "WHERE device_id=? AND boot_id=? AND received_at>=?",
                        (session["device_id"], latest["boot_id"], session["started_at"]),
                    ).fetchone()
                    raw_max = 0 if raw_row is None or raw_row["max_seq"] is None else int(raw_row["max_seq"])
            else:
                row = self._conn.execute(
                    "SELECT MAX(sequence) AS max_seq FROM raw_samples "
                    "WHERE device_id=? AND boot_id=? "
                    "AND (? IS NULL OR sequence>=?)",
                    (
                        session["device_id"],
                        session["start_boot_id"],
                        session["start_sequence"],
                        session["start_sequence"],
                    ),
                ).fetchone()
                raw_max = 0 if row is None or row["max_seq"] is None else int(row["max_seq"])
            assigned_max = self._conn.execute(
                "SELECT MAX(sequence) AS max_seq FROM session_samples WHERE session_id=?",
                (session_id,),
            ).fetchone()
        assigned = 0 if assigned_max is None or assigned_max["max_seq"] is None else int(assigned_max["max_seq"])
        return max(raw_max, assigned)

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

        - sample_count/raw_count: số mẫu boundary/raw đã gán.
        - processed_count/processed_coverage: số và tỷ lệ có assessment.
        - timing_span_s/mean_cadence_hz/max_timing_gap_s: nhịp monotonic.
        - late_count: mẫu gán có is_late=1 trong raw_samples.
        - gap_count: tổng độ dài các khoảng sequence thiếu giữa min-max của
          các mẫu đã gán, tính riêng từng boot.
        - violation/clear/unknown_count: đếm theo ``safety.status`` trong
          processed_samples join session_samples; chỉ đếm theo
          ``processor_version`` đã cho, ``None`` = không lọc (mọi version).
        - activity: bằng chứng distance-activity-v1 từ raw và assessment đúng
          version; ``None`` chọn live PROCESSOR_VERSION chỉ cho activity.
        """
        # Legacy None still counts every processed version. Activity always
        # requires exactly one version; resolve the live default here to avoid
        # the service -> Store import cycle.
        if processor_version is None:
            from iot_backend.ingestion.service import PROCESSOR_VERSION
            activity_version = PROCESSOR_VERSION
        else:
            activity_version = processor_version
        with self._lock:
            session = self._conn.execute(
                "SELECT device_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            sample_rows = self._conn.execute(
                "SELECT ss.device_id, ss.boot_id, ss.sequence, r.id AS raw_id, "
                "r.is_late, r.monotonic_us, r.received_at, r.measured_at, "
                "r.time_synced, r.distance_mm, r.valid, "
                "p.result_json AS activity_assessment_json "
                "FROM session_samples ss "
                "LEFT JOIN raw_samples r "
                  "ON r.device_id=ss.device_id AND r.boot_id=ss.boot_id "
                 "AND r.sequence=ss.sequence "
                "LEFT JOIN processed_samples p "
                  "ON p.device_id=r.device_id AND p.boot_id=r.boot_id "
                 "AND p.sequence=r.sequence AND p.processor_version=? "
                "WHERE ss.session_id=? ORDER BY ss.boot_id, ss.sequence",
                (activity_version, session_id),
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
            if processor_version is None:
                event_rows = self._conn.execute(
                    "SELECT event_json FROM event_packages WHERE session_id=?",
                    (session_id,),
                ).fetchall()
            else:
                event_rows = self._conn.execute(
                    "SELECT event_json FROM event_packages WHERE session_id=? "
                    "AND processor_version=?",
                    (session_id, processor_version),
                ).fetchall()

        late_count = sum(1 for row in sample_rows if row["is_late"])
        raw_count = sum(1 for row in sample_rows if row["is_late"] is not None)
        gap_count = 0
        prev_boot: str | None = None
        prev_seq: int | None = None
        for row in sample_rows:
            if row["boot_id"] != prev_boot:
                prev_boot, prev_seq = row["boot_id"], row["sequence"]
                continue
            gap_count += row["sequence"] - prev_seq - 1
            prev_seq = row["sequence"]

        monotonic_by_boot: dict[str, list[int]] = {}
        for row in sample_rows:
            value = row["monotonic_us"]
            if value is not None:
                monotonic_by_boot.setdefault(row["boot_id"], []).append(int(value))
        timing_intervals = [
            (current - previous) / 1_000_000
            for values in monotonic_by_boot.values()
            for previous, current in zip(values, values[1:])
            if current > previous
        ]
        timing_span_s = max(
            (max(values) - min(values)) / 1_000_000
            for values in monotonic_by_boot.values()
            if len(values) > 1
        ) if any(len(values) > 1 for values in monotonic_by_boot.values()) else 0.0
        max_timing_gap_s = max(timing_intervals, default=0.0)
        mean_cadence_hz = (
            (len(timing_intervals) / sum(timing_intervals))
            if timing_intervals and sum(timing_intervals) > 0 else None
        )

        counts = {STATUS_CLEAR: 0, STATUS_VIOLATION: 0, STATUS_UNKNOWN: 0}
        for row in processed_rows:
            try:
                record = json.loads(row["result_json"])
            except ValueError:
                continue
            safety = record.get("safety") if isinstance(record, dict) else None
            status = safety.get("status") if isinstance(safety, dict) else None
            if isinstance(status, str) and status in counts:
                counts[status] += 1

        confirmed_violation_count = 0
        observed_violation_count = 0
        for row in event_rows:
            try:
                event = json.loads(row["event_json"])
            except ValueError:
                continue
            if event.get("event_type") != "VIOLATION":
                continue
            if event.get("confirmation_status", "CONFIRMED") == "CONFIRMED":
                confirmed_violation_count += 1
            else:
                observed_violation_count += 1

        return {
            "sample_count": len(sample_rows),
            "raw_count": raw_count,
            "processed_count": len(processed_rows),
            "processed_coverage": (
                len(processed_rows) / raw_count if raw_count else 0.0
            ),
            "late_count": late_count,
            "gap_count": gap_count,
            "timing_span_s": timing_span_s,
            "mean_cadence_hz": mean_cadence_hz,
            "max_timing_gap_s": max_timing_gap_s,
            "violation_count": counts[STATUS_VIOLATION],
            "clear_count": counts[STATUS_CLEAR],
            "unknown_count": counts[STATUS_UNKNOWN],
            "confirmed_violation_count": confirmed_violation_count,
            "observed_violation_count": observed_violation_count,
            "activity": compute_session_activity(
                (row for row in sample_rows if row["raw_id"] is not None),
                session_id=session_id,
                device_id=session["device_id"] if session is not None else None,
                processor_version=activity_version,
            ),
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
                "ORDER BY received_at DESC, id DESC LIMIT 1",
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

    def latest_processed(self, device_id: str, processor_version: str) -> sqlite3.Row | None:
        """Newest processed assessment by source receive time for one device."""
        with self._lock:
            return self._conn.execute(
                """SELECT p.* FROM raw_samples r INDEXED BY idx_raw_device_received
                   JOIN processed_samples p INDEXED BY idx_processed_key
                     ON p.device_id=r.device_id
                    AND p.boot_id=r.boot_id AND p.sequence=r.sequence
                   WHERE r.device_id=? AND p.processor_version=?
                   ORDER BY r.received_at DESC, r.id DESC LIMIT 1""",
                (device_id, processor_version),
            ).fetchone()

    def recent_processed(self, device_id: str, processor_version: str, limit: int = 600) -> list[sqlite3.Row]:
        """Processed assessments in source receive order, oldest first."""
        with self._lock:
            return self._conn.execute(
                """SELECT p.* FROM raw_samples r INDEXED BY idx_raw_device_received
                   JOIN processed_samples p INDEXED BY idx_processed_key
                     ON p.device_id=r.device_id
                    AND p.boot_id=r.boot_id AND p.sequence=r.sequence
                   WHERE r.device_id=? AND p.processor_version=?
                   ORDER BY r.received_at DESC, r.id DESC LIMIT ?""",
                (device_id, processor_version, limit),
            ).fetchall()[::-1]

    def list_model_registry(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM model_registry ORDER BY activated_at DESC").fetchall()

    def insert_edge_prediction(self, payload: dict[str, Any], received_at: str) -> bool:
        ref = payload["sample_ref"]
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO edge_predictions
                   (device_id,boot_id,sequence,received_at,model_version,model_sha256,payload_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (ref["device_id"], ref["boot_id"], ref["sequence"], received_at,
                 payload["model_version"], payload["model_sha256"], json.dumps(payload, sort_keys=True)),
            )
            return cur.rowcount > 0

    def edge_prediction(self, device_id: str, boot_id: str, sequence: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM edge_predictions WHERE device_id=? AND boot_id=? AND sequence=? ORDER BY received_at DESC LIMIT 1",
                (device_id, boot_id, sequence),
            ).fetchone()

    # -------------------------------------------------------------- events

    def event_state(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM event_detector_state WHERE state_key=?",(key,)
            ).fetchone()
            return None if row is None else json.loads(row["state_json"])

    def save_event_state(self, key: str, state: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO event_detector_state VALUES (?,?) ON CONFLICT(state_key) "
                "DO UPDATE SET state_json=excluded.state_json",
                (key,json.dumps(state,allow_nan=False)),
            )

    def insert_event(self, event: dict) -> bool:
        ref=event["trigger_ref"]
        with self._lock:
            cur=self._conn.execute(
                "INSERT OR IGNORE INTO event_packages VALUES (?,?,?,?,?,?,?)",
                (event["event_id"],ref["device_id"],ref["boot_id"],event["session_id"],
                 event["processor_version"],ref["sequence"],json.dumps(event,allow_nan=False)),
            )
            if cur.rowcount and event.get("chain_eligible", True):
                payload = self._chain_payload(event)
                payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                now = utc_now_iso()
                self._conn.execute(
                    """INSERT OR IGNORE INTO chain_event_outbox
                       (event_id,payload_json,payload_hash,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (event["event_id"], payload_json, payload_hash, "OUTBOX_PENDING", now, now),
                )
            return cur.rowcount > 0

    def confirm_event(
        self, event_id: str, *, confirmed_ref: dict[str, Any], evidence_count: int
    ) -> bool:
        """Promote an observed violation and create its immutable outbox row."""
        if evidence_count < 2:
            raise ValueError("violation confirmation needs at least two samples")
        with self._lock:
            row = self._conn.execute(
                "SELECT event_json FROM event_packages WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            event = json.loads(row["event_json"])
            if event.get("event_type") != "VIOLATION":
                return False
            if event.get("confirmation_status") == "CONFIRMED":
                return True
            event["confirmed_ref"] = confirmed_ref
            event["confirmation_status"] = "CONFIRMED"
            event["confirmation_rule"] = "2_of_3_valid_consecutive"
            event["evidence_count"] = int(evidence_count)
            event["chain_eligible"] = True
            self._conn.execute(
                "UPDATE event_packages SET event_json=? WHERE event_id=?",
                (json.dumps(event, allow_nan=False), event_id),
            )
            payload = self._chain_payload(event)
            payload_json = json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            now = utc_now_iso()
            self._conn.execute(
                """INSERT OR IGNORE INTO chain_event_outbox
                   (event_id,payload_json,payload_hash,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (event_id, payload_json, payload_hash, "OUTBOX_PENDING", now, now),
            )
            return True

    @staticmethod
    def _chain_payload(event: dict) -> dict:
        assessment = event["assessment"]
        safety = assessment["safety"]
        prediction = assessment["prediction"]
        source_event_type = event["event_type"]
        # The HTTP blockchain contract uses WARNING as its canonical state.
        # Keep the richer IoT event name as metadata instead of sending an
        # unsupported state value across the service boundary.
        state = "WARNING" if source_event_type == "PREDICTED_WARNING" else source_event_type
        risk = {"WARNING": 2, "UNKNOWN": 2, "VIOLATION": 3}.get(state, 2)
        measured_at = assessment.get("measured_at") or assessment.get("computed_at")
        from iot_backend.util import parse_iso_utc
        timestamp = int(parse_iso_utc(measured_at).timestamp())
        ai_result = {
            "state": state,
            "sourceEventType": source_event_type,
            "riskLevel": risk,
        }
        for target, source in (
            ("confirmationStatus", event.get("confirmation_status")),
            ("confirmationRule", event.get("confirmation_rule")),
            ("evidenceCount", event.get("evidence_count")),
            ("earlyWarning", prediction.get("early_warning")),
            ("predictedViolationMs", prediction.get("predicted_violation_ms")),
            ("model", prediction.get("model_version")),
        ):
            if source is not None:
                ai_result[target] = source
        payload = {
            "schemaVersion": "1.0",
            "eventId": event["event_id"],
            "deviceId": assessment["sample_ref"]["device_id"],
            "bootId": assessment["sample_ref"]["boot_id"],
            "sequence": assessment["sample_ref"]["sequence"],
            "timestamp": timestamp,
            "sensorData": {
                "distance": assessment.get("distance_m"),
                "valid": safety.get("sensor_valid"),
                "safetyThreshold": safety.get("sp_m"),
                "margin": safety.get("margin_m"),
            },
            "aiResult": ai_result,
        }
        if event.get("session_id") is not None:
            payload["sessionId"] = event["session_id"]
        return payload

    def get_chain_outbox(self, event_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM chain_event_outbox WHERE event_id=?", (event_id,)).fetchone()

    def due_chain_outbox(self, limit: int = 20) -> list[sqlite3.Row]:
        now = utc_now_iso()
        with self._lock:
            return self._conn.execute(
                """SELECT * FROM chain_event_outbox
                   WHERE status IN ('OUTBOX_PENDING','RETRYABLE_ERROR','SUBMITTED')
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                   ORDER BY created_at LIMIT ?""", (now, limit)
            ).fetchall()

    def update_chain_outbox(self, event_id: str, **fields: Any) -> None:
        allowed = {"status", "attempts", "next_attempt_at", "transaction_hash", "block_number", "chain_data_hash", "last_http_status", "last_error", "confirmed_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported chain outbox fields: {sorted(unknown)}")
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE chain_event_outbox SET {assignments} WHERE event_id=?",
                (*fields.values(), event_id),
            )

    def list_events(self, processor_version: str, session_id: str | None = None,
                    limit: int = 1000) -> list[dict]:
        with self._lock:
            rows=self._conn.execute(
                "SELECT event_json FROM event_packages WHERE processor_version=? "
                "AND (? IS NULL OR session_id=?) ORDER BY device_id,boot_id,trigger_sequence LIMIT ?",
                (processor_version,session_id,session_id,limit),
            ).fetchall()
        return [json.loads(r["event_json"]) for r in rows]

    def event_package(self, event_id: str) -> dict | None:
        """Raw coverage is computed from processed samples of the same scope.

        Waiting packages become ready as post samples are processed. Session
        boundaries filter raw using stored assessments, including session=None;
        event trigger/first assessment never changes. Late raw can fill gaps.
        """
        with self._lock:
            row=self._conn.execute("SELECT event_json FROM event_packages WHERE event_id=?",(event_id,)).fetchone()
            if row is None:
                return None
            event=json.loads(row["event_json"])
            ref=event["trigger_ref"]
            rows=self._conn.execute(
                "SELECT r.* FROM raw_samples r JOIN processed_samples p "
                "ON r.device_id=p.device_id AND r.boot_id=p.boot_id AND r.sequence=p.sequence "
                "WHERE r.device_id=? AND r.boot_id=? AND p.processor_version=? "
                "AND json_extract(p.result_json,'$.session_id') IS ? ORDER BY r.sequence",
                (ref["device_id"],ref["boot_id"],event["processor_version"],event["session_id"]),
            ).fetchall()
        start=event["trigger_monotonic_us"] - event["pre_ms"]*1000
        end=event["trigger_monotonic_us"] + event["post_ms"]*1000
        # Include nearest samples bracketing the requested interval. With
        # real sampling jitter, selecting only points inside [start,end]
        # would never provide a full two/one seconds of measured coverage.
        earlier=[r["monotonic_us"] for r in rows
                 if r["monotonic_us"] <= start and r["sequence"] <= ref["sequence"]]
        later=[r["monotonic_us"] for r in rows
               if r["monotonic_us"] >= end and r["sequence"] >= ref["sequence"]]
        lower=max(earlier) if earlier else start
        upper=min(later) if later else end
        selected=[r for r in rows if lower <= r["monotonic_us"] <= upper]
        before=[r for r in selected if r["sequence"] <= ref["sequence"]]
        after=[r for r in selected if r["sequence"] >= ref["sequence"]]
        sequences=[r["sequence"] for r in selected]
        gaps=sum(max(0,b-a-1) for a,b in zip(sequences,sequences[1:]))
        timing_gaps=sum(not (abs((b["monotonic_us"]-a["monotonic_us"])/1e6 - .1) <= .03)
                        for a,b in zip(selected,selected[1:]))
        pre_span=(event["trigger_monotonic_us"]-min(r["monotonic_us"] for r in before))/1000 if before else 0
        post_span=(max(r["monotonic_us"] for r in after)-event["trigger_monotonic_us"])/1000 if after else 0
        pre_complete=pre_span >= event["pre_ms"]
        post_complete=post_span >= event["post_ms"]
        # If source has advanced beyond the requested post window, no more
        # in-order post samples can arrive; gaps remain explicit.
        settled=bool(rows and max(r["monotonic_us"] for r in rows) >= end)
        if event["session_id"] is not None:
            session = self.get_session(event["session_id"])
            settled = settled or bool(session is not None and session["status"] == "closed")
        event["raw_samples"]=[json.loads(r["payload_json"]) for r in selected]
        event["coverage"]={
            "pre_complete":pre_complete,"post_complete":post_complete,
            "pre_available_ms":pre_span,"post_available_ms":post_span,
            "sequence_gap_count":gaps,"timing_gap_count":timing_gaps,
            "complete":pre_complete and post_complete and gaps==0 and timing_gaps==0,
        }
        event["package_status"]="ready" if settled else "collecting"
        return event

    # ------------------------------------------------------------------ khác

    def close(self) -> None:
        """Đóng kết nối SQLite."""
        with self._lock:
            self._conn.close()
