"""Train model v1 (M5c): RandomForestRegressor multi-output + artifact manifest.

Pipeline ``train_model`` (API hàm + CLI ``python -m ai_model.train``):

1. Nạp YAML cấu hình (``configs/model.yaml``) → ``WindowConfig`` (windows.py)
   + seed + phiên bản model/preprocessing.
2. ``load_jsonl_dataset`` (prepare.py) đọc JSONL synthetic; mỗi row được gắn
   ``session_id = meta["session_label"]`` để windowing tách theo session
   (payload telemetry_v1 không có trường session_id).
3. ``extract_windows`` → cửa sổ X (10 khoảng cách) + y (5 horizon) + stats.
4. ``split_by_session`` trên session của cửa sổ (ratios mặc định 70/15/15,
   seed từ config); kiểm không leakage: giao session giữa các split phải
   rỗng và mọi cửa sổ thuộc đúng một split.
5. Hyperparameter search NHỎ trên validation: grid ``n_estimators ×
   max_depth`` (mặc định ``[50,100,200] × [None,8]`` — 6 tổ hợp, duyệt theo
   thứ tự grid), mỗi tổ hợp fit ``RandomForestRegressor(n_estimators,
   max_depth, random_state=seed)`` trên train (multi-output tự nhiên qua y
   2D), đo MAE trung bình 5 horizon trên val; chọn tổ hợp MAE thấp nhất
   (hòa → tổ hợp ĐẦU theo thứ tự grid). Test không đụng đến trong bước này.
6. Refit model tốt nhất trên train+val — thực hành chuẩn: sau khi chọn
   hyperparameter bằng validation, tận dụng thêm dữ liệu val cho model cuối;
   test chỉ dùng để báo cáo ở M5d. Model fit trên pandas DataFrame với tên
   cột = feature_names → sklearn lưu ``feature_names_in_`` đúng contract.
7. Ghi artifact vào ``out_dir`` (mặc định ``ai_model/artifacts/``):
   ``model.joblib`` (joblib.dump) + ``manifest.json`` đủ
   ``REQUIRED_MANIFEST_FIELDS``. ``split_manifest["created_at"]`` là giá trị
   DẪN XUẤT TỪ SEED (epoch + seed ms — KHÔNG phải giờ chạy thật) để sha256
   của split manifest ổn định giữa các lần chạy cùng seed; model manifest
   có ``created_at`` thật riêng (thời điểm ghi artifact).
8. Trả về summary dict; CLI in JSON ra stdout.

``load_model`` chỉ nạp artifact từ đường dẫn cấu hình TIN CẬY do operator
chỉ định (env ``MODEL_ARTIFACT_DIR`` / tham số đã kiểm soát) và kiểm manifest
đủ field + khớp ``FEATURE_NAMES`` trước khi trả — xem docstring hàm.

Chỉ dùng 10 khoảng cách làm feature (không sequence/session/scenario/kết quả
tương lai) theo PLAN.md mục 4.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn
import yaml
from sklearn.ensemble import RandomForestRegressor

from ai_model.features import FEATURE_NAMES, feature_names, window_to_features
from ai_model.prepare import (
    SPLIT_NAMES,
    build_split_manifest,
    load_jsonl_dataset,
    split_by_session,
)
from ai_model.windows import WindowConfig, extract_windows

__all__ = [
    "DEFAULT_N_ESTIMATORS_GRID",
    "DEFAULT_MAX_DEPTH_GRID",
    "REQUIRED_MANIFEST_FIELDS",
    "train_model",
    "load_model",
    "main",
]

# Grid mặc định PLAN.md mục 4 — 6 tổ hợp, duyệt theo thứ tự: n_estimators
# ngoài, max_depth trong. Hòa MAE → tổ hợp đầu tiên theo thứ tự này thắng.
DEFAULT_N_ESTIMATORS_GRID: tuple[int, ...] = (50, 100, 200)
DEFAULT_MAX_DEPTH_GRID: tuple[int | None, ...] = (None, 8)

# Field bắt buộc của manifest.json (artifact model v1). load_model từ chối
# nạp nếu thiếu bất kỳ field nào.
REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "feature_names",
    "units",
    "window_size",
    "delta_s",
    "horizons_ms",
    "max_time_jitter_s",
    "max_gap_s",
    "preprocessing_version",
    "model_version",
    "random_seed",
    "sklearn_version",
    "pandas_version",
    "numpy_version",
    "joblib_version",
    "split_manifest",
    "split_manifest_sha256",
    "n_train_windows",
    "n_val_windows",
    "n_test_windows",
    "best_params",
    "val_mae_per_horizon",
    "created_at",
)

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ALLOWED_OVERRIDES = ("seed", "n_estimators_grid", "max_depth_grid")


def _iso_utc_ms(dt: datetime) -> str:
    """ISO-8601 UTC millisecond, hậu tố Z (khớp prepare.py/_utc_now_iso)."""
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seed_derived_created_at(seed: int) -> str:
    """``created_at`` DẪN XUẤT TỪ SEED cho split manifest (deterministic).

    Epoch + ``seed`` millisecond — cùng seed cho cùng giá trị nên sha256 của
    split manifest (và toàn bộ manifest trừ ``created_at`` thật) giống nhau
    giữa các lần chạy. Đây KHÔNG phải thời điểm train thật.
    """
    return _iso_utc_ms(_EPOCH_UTC + timedelta(milliseconds=seed))


def _resolve_params(
    config: Mapping[str, Any], overrides: Mapping[str, Any]
) -> tuple[int, list[int], list[int | None]]:
    """Gộp config YAML + overrides → (seed, n_estimators_grid, max_depth_grid).

    Override key lạ → ValueError (chặn typo âm thầm); grid rỗng hoặc giá trị
    không hợp lệ → ValueError. Không fallback cứng cho key config thiếu.
    """
    unknown = sorted(set(overrides) - set(_ALLOWED_OVERRIDES))
    if unknown:
        raise ValueError(
            f"config_overrides có key không hỗ trợ: {unknown}; "
            f"chỉ nhận {list(_ALLOWED_OVERRIDES)}"
        )
    seed = int(overrides.get("seed", config["random_seed"]))
    n_estimators_grid = [
        int(value)
        for value in overrides.get("n_estimators_grid", DEFAULT_N_ESTIMATORS_GRID)
    ]
    if any(value < 1 for value in n_estimators_grid):
        raise ValueError(f"n_estimators phải >= 1, nhận {n_estimators_grid}")
    max_depth_grid: list[int | None] = []
    for value in overrides.get("max_depth_grid", DEFAULT_MAX_DEPTH_GRID):
        if value is None:
            max_depth_grid.append(None)
        else:
            depth = int(value)
            if depth < 1:
                raise ValueError(f"max_depth phải >= 1 hoặc None, nhận {value}")
            max_depth_grid.append(depth)
    if not n_estimators_grid or not max_depth_grid:
        raise ValueError("grid hyperparameter rỗng — cần ít nhất 1 tổ hợp")
    return seed, n_estimators_grid, max_depth_grid


def _assert_no_leakage(split: Mapping[str, set[str]], windows: Sequence[Any]) -> None:
    """Kiểm leakage split: giao session giữa các split rỗng và mọi cửa sổ
    thuộc đúng một split. Vi phạm → ValueError (không chạy tiếp)."""
    train, val, test = (set(split[name]) for name in SPLIT_NAMES)
    overlap = (train & val) | (train & test) | (val & test)
    if overlap:
        raise ValueError(f"leakage: session xuất hiện ở nhiều split: {sorted(overlap)}")
    window_sessions = {w.meta.session_id for w in windows}
    unassigned = window_sessions - (train | val | test)
    if unassigned:
        raise ValueError(f"session không thuộc split nào: {sorted(unassigned)}")


def train_model(
    dataset_path: str | Path,
    config_path: str | Path = "configs/model.yaml",
    out_dir: str | Path = "ai_model/artifacts",
    config_overrides: Mapping[str, Any] | None = None,
) -> dict:
    """Train RF multi-output theo pipeline module docstring; trả summary dict.

    Tham số:
      - ``dataset_path``: file JSONL sinh bởi ``scripts/synthetic_dataset.py``
        (mỗi dòng có nhãn ``session_label`` — bắt buộc để split theo session).
      - ``config_path``: YAML cấu hình (window + seed + phiên bản).
      - ``out_dir``: thư mục ghi ``model.joblib`` + ``manifest.json``.
      - ``config_overrides``: dict tùy chọn — ``seed``, ``n_estimators_grid``,
        ``max_depth_grid`` (grid rút gọn cho test/experiment). Key lạ → lỗi.

    Summary gồm: số dòng, window stats, session từng split, số cửa sổ từng
    split, bảng kết quả grid (MAE trung bình 5 horizon trên val của từng tổ
    hợp), best_params, val MAE per horizon của model tốt nhất, đường dẫn
    artifact. Mọi metric lấy từ lần chạy thật của lần gọi này.
    """
    overrides = dict(config_overrides or {})
    with Path(config_path).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    window_config = WindowConfig.from_dict(config)
    seed, n_estimators_grid, max_depth_grid = _resolve_params(config, overrides)
    feat_names = feature_names(config)

    # (1)-(2) Dữ liệu: payload + gắn session_id từ nhãn session_label.
    records = load_jsonl_dataset(dataset_path)
    rows: list[dict] = []
    for record in records:
        row = dict(record["row"])
        row["session_id"] = record["meta"].get("session_label")
        rows.append(row)

    # (3) Cửa sổ.
    windows, stats = extract_windows(rows, window_config)
    if not windows:
        raise ValueError(
            f"{dataset_path} không tạo được cửa sổ nào (window_size="
            f"{window_config.window_size}, horizon cuối cần "
            f"{window_config.window_size + max(window_config.target_offsets)} mẫu)"
        )

    # (4) Split theo session của cửa sổ + kiểm leakage.
    split = split_by_session(windows, seed=seed)
    _assert_no_leakage(split, windows)
    split_of: dict[str, str] = {}
    for name in SPLIT_NAMES:
        for session in split[name]:
            split_of[session] = name

    x_rows: dict[str, list[list[float]]] = {name: [] for name in SPLIT_NAMES}
    y_rows: dict[str, list[list[float]]] = {name: [] for name in SPLIT_NAMES}
    for window in windows:
        session = window.meta.session_id
        if session not in split_of:
            raise ValueError(f"session {session!r} của một cửa sổ không thuộc split nào")
        name = split_of[session]
        x_rows[name].append(window_to_features(window))
        y_rows[name].append([float(target) for target in window.targets])

    x_arrays = {
        name: np.asarray(x_rows[name], dtype=float) for name in SPLIT_NAMES
    }
    y_arrays = {
        name: np.asarray(y_rows[name], dtype=float) for name in SPLIT_NAMES
    }
    for name in SPLIT_NAMES:
        if x_arrays[name].shape[0] == 0:
            raise ValueError(
                f"split '{name}' không có cửa sổ nào — dataset quá nhỏ cho "
                "ratios hiện tại"
            )

    # (5) Hyperparameter search trên validation. Fit bằng DataFrame để sklearn
    # ghi nhận tên feature; predict cũng bằng DataFrame (khớp feature names).
    x_train_df = pd.DataFrame(x_arrays["train"], columns=feat_names)
    x_val_df = pd.DataFrame(x_arrays["val"], columns=feat_names)
    grid_results: list[dict] = []
    best: dict | None = None
    for n_estimators in n_estimators_grid:
        for max_depth in max_depth_grid:
            model = RandomForestRegressor(
                n_estimators=n_estimators,
                max_depth=max_depth,
                random_state=seed,
            )
            model.fit(x_train_df, y_arrays["train"])
            prediction = model.predict(x_val_df)
            mae_per_horizon = np.mean(
                np.abs(prediction - y_arrays["val"]), axis=0
            )
            entry = {
                "n_estimators": int(n_estimators),
                "max_depth": max_depth,
                "val_mae_per_horizon": [float(v) for v in mae_per_horizon],
                "val_mae_mean": float(np.mean(mae_per_horizon)),
            }
            grid_results.append(entry)
            # Hòa (==) → giữ tổ hợp ĐẦU theo thứ tự grid (chỉ thay khi thấp hơn).
            if best is None or entry["val_mae_mean"] < best["val_mae_mean"]:
                best = entry
    assert best is not None  # grid khác rỗng đã đảm bảo ở _resolve_params

    # (6) Refit tốt nhất trên train+val (lý do xem module docstring bước 6).
    x_train_val = np.vstack([x_arrays["train"], x_arrays["val"]])
    y_train_val = np.vstack([y_arrays["train"], y_arrays["val"]])
    final_model = RandomForestRegressor(
        n_estimators=best["n_estimators"],
        max_depth=best["max_depth"],
        random_state=seed,
    )
    final_model.fit(pd.DataFrame(x_train_val, columns=feat_names), y_train_val)
    if list(getattr(final_model, "feature_names_in_", [])) != list(feat_names):
        raise RuntimeError(
            "feature_names_in_ của model không khớp feature_names cấu hình"
        )

    # (7) Artifact: split manifest có created_at dẫn xuất từ seed (deterministic).
    split_manifest = build_split_manifest(
        split, seed=seed, created_at=_seed_derived_created_at(seed)
    )
    split_manifest_sha256 = hashlib.sha256(
        json.dumps(split_manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()

    from ai_model.preprocessing import sha256_file
    manifest = {
        "dataset_sha256": sha256_file(dataset_path),
        "feature_names": list(feat_names),
        "units": "m",
        "window_size": window_config.window_size,
        "delta_s": window_config.delta_s,
        "horizons_ms": list(window_config.horizons_ms),
        "max_time_jitter_s": window_config.max_time_jitter_s,
        "max_gap_s": window_config.max_gap_s,
        "preprocessing_version": str(config["preprocessing_version"]),
        "model_version": str(config["model_version"]),
        "random_seed": seed,
        "sklearn_version": sklearn.__version__,
        "pandas_version": pd.__version__,
        "numpy_version": np.__version__,
        "joblib_version": joblib.__version__,
        "split_manifest": split_manifest,
        "split_manifest_sha256": split_manifest_sha256,
        "n_train_windows": int(x_arrays["train"].shape[0]),
        "n_val_windows": int(x_arrays["val"].shape[0]),
        "n_test_windows": int(x_arrays["test"].shape[0]),
        "best_params": {
            "n_estimators": best["n_estimators"],
            "max_depth": best["max_depth"],
        },
        "val_mae_per_horizon": best["val_mae_per_horizon"],
        "created_at": _iso_utc_ms(datetime.now(timezone.utc)),
    }

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model_path = out / "model.joblib"
    manifest_path = out / "manifest.json"
    joblib.dump(final_model, model_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # (8) Summary — mọi metric từ lần chạy thật của lần gọi này.
    return {
        "dataset": str(dataset_path),
        "config": str(config_path),
        "out_dir": str(out),
        "model_version": manifest["model_version"],
        "preprocessing_version": manifest["preprocessing_version"],
        "random_seed": seed,
        "n_rows": len(rows),
        "window_stats": asdict(stats),
        "split_sessions": {name: sorted(split[name]) for name in SPLIT_NAMES},
        "n_windows": {
            "train": manifest["n_train_windows"],
            "val": manifest["n_val_windows"],
            "test": manifest["n_test_windows"],
        },
        "grid": grid_results,
        "best_params": manifest["best_params"],
        "val_mae_per_horizon": manifest["val_mae_per_horizon"],
        "val_mae_mean": best["val_mae_mean"],
        "artifacts": {"model": str(model_path), "manifest": str(manifest_path)},
        "created_at": manifest["created_at"],
    }


def load_model(out_dir: str | Path) -> tuple[Any, dict]:
    """Tải ``(model, manifest)`` từ artifact dir — ĐƯỜNG DẪN CẤU HÌNH TIN CẬY.

    Artifact chỉ được nạp từ đường dẫn do operator cấu hình (env
    ``MODEL_ARTIFACT_DIR`` / tham số lệnh đã kiểm soát) — hàm này KHÔNG tự dò
    tìm artifact từ nguồn lạ. Trước khi trả, kiểm:

    - ``model.joblib`` + ``manifest.json`` tồn tại (thiếu → FileNotFoundError);
    - manifest đủ mọi field ``REQUIRED_MANIFEST_FIELDS`` (thiếu → ValueError:
      artifact hỏng hoặc ghi bởi phiên bản không tương thích);
    - ``manifest["feature_names"]`` khớp ``FEATURE_NAMES`` của
      ``ai_model/features.py`` (lệch → ValueError: chặn trường hợp nạp model
      train với thứ tự feature khác rồi dự báo sai âm thầm).
    """
    directory = Path(out_dir)
    model_path = directory / "model.joblib"
    manifest_path = directory / "manifest.json"
    if not model_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(
            f"artifact không đầy đủ trong {directory}: cần cả "
            f"{model_path.name} và {manifest_path.name}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise ValueError(f"manifest thiếu field bắt buộc: {missing}")
    if list(manifest["feature_names"]) != list(FEATURE_NAMES):
        raise ValueError(
            f"manifest feature_names {manifest['feature_names']} không khớp "
            f"FEATURE_NAMES {FEATURE_NAMES} — từ chối nạp"
        )
    return joblib.load(model_path), manifest


def main(argv: list[str] | None = None) -> int:
    """CLI train: ``python -m ai_model.train --dataset <file.jsonl>``; in
    summary JSON ra stdout."""
    parser = argparse.ArgumentParser(
        description=(
            "Train RandomForest multi-output (model v1) từ dataset JSONL "
            "synthetic; ghi model.joblib + manifest.json vào out-dir và in "
            "summary JSON ra stdout."
        )
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Đường dẫn file JSONL dataset (bắt buộc)",
    )
    parser.add_argument(
        "--config",
        default="configs/model.yaml",
        help="File YAML cấu hình (mặc định: configs/model.yaml)",
    )
    parser.add_argument(
        "--out-dir",
        default="ai_model/artifacts",
        help="Thư mục artifact đầu ra (mặc định: ai_model/artifacts)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Ghi đè random_seed trong config (mặc định: lấy từ config)",
    )
    args = parser.parse_args(argv)
    overrides = {"seed": args.seed} if args.seed is not None else None
    summary = train_model(
        args.dataset, args.config, args.out_dir, config_overrides=overrides
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
