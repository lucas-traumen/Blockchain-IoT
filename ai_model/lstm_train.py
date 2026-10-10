"""Train pipeline LSTM (M11): Keras/TensorFlow trong environment AI riêng.

Pipeline ``train_lstm`` (API hàm và CLI của model LSTM duy nhất):

1. Nạp YAML cấu hình (khối ``lstm`` + windowing dùng chung) → ``WindowConfig``
   + kiến trúc/optimizer/callback. Override key lạ → ``ValueError``.
2. ``load_jsonl_dataset`` đọc JSONL (session_label ở meta — M10 tự xử lý
   session, module này KHÔNG tự ghép session để tránh leakage path riêng).
3. ``build_lstm_dataset`` (M10): split theo session TRƯỚC, windowing SAU;
   guard split rỗng/tổng 0 window đã có ở M10.
4. ``LSTMScaler.fit`` CHỈ trên ``ds.train`` (X và y); transform X/y mọi
   split bằng tham số train-only.
5. Fit Keras: Input (10,2) → LSTM(32) → Dropout(0.2) → Dense(32, relu) →
   Dense(5); Adam lr 1e-3; Huber; callbacks EarlyStopping/ReduceLROnPlateau/
   ModelCheckpoint đọc từ config. Validation dùng cho callbacks/model
   selection; test KHÔNG đụng đến trong fit.
6. Metrics TÍNH SAU inverse y về mét: MAE per horizon cho train/val/test,
   loss/mae scaled từ ``model.evaluate``, history từ fit. JSON ghi với
   ``allow_nan=False`` — NaN/Infinity lọt vào → ValueError ngay.
7. Artifact ghi ATOMIC-ISH: fit xong mới ghi vào thư mục staging trong
   ``out_dir``, validate manifest bằng schema + parse timestamp, rồi
   ``os.replace`` từng file vào ``out_dir``; lỗi giữa chừng → dọn staging,
   không để artifact dở.

5 artifact bắt buộc trong ``out_dir``: ``model.keras``, ``scaler.json``,
``split_manifest.json``, ``manifest.json``, ``metrics.json``.

``load_lstm_artifact`` nạp lại ``(model, scaler, manifest)`` từ đường dẫn
operator tin cậy: kiểm đủ file, SHA-256 khớp bytes thực, manifest qua schema
 Draft 2020-12 + ``created_at`` parse được, shape/feature/version khớp
contract — lệch bất kỳ → exception, KHÔNG fallback âm thầm.

TensorFlow import LAZY (chỉ trong ``_import_tf``) — import module này không
cần TensorFlow (dùng được trong iot_backend ``.venv`` cho validate/override).
Kiến trúc đọc từ khối ``lstm`` của config; loss/optimizer chỉ chấp nhận giá
trị contract (``huber``/``adam``) — giá trị khác → ``ValueError``, không
im lặng dùng cái khác.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from ai_model.prepare import (
    SPLIT_NAMES,
    build_lstm_dataset,
    load_jsonl_dataset,
)
from ai_model.preprocessing import (
    PP_LSTM_VERSION,
    LSTMScaler,
    canonical_json,
    sha256_file,
    write_scaler_json,
    write_split_manifest,
)
from ai_model.windows import WindowConfig

__all__ = [
    "ALLOWED_LSTM_OVERRIDES",
    "REQUIRED_LSTM_ARTIFACTS",
    "LSTM_FEATURES",
    "LSTM_INPUT_SHAPE",
    "LSTM_OUTPUT_SHAPE",
    "MANIFEST_SCHEMA_PATH",
    "build_lstm_model",
    "validate_manifest_document",
    "validate_lstm_config",
    "train_lstm",
    "load_lstm_artifact",
]

# Override cho phép (key lạ → ValueError, chặn typo âm thầm).
ALLOWED_LSTM_OVERRIDES: tuple[str, ...] = (
    "seed",
    "max_epochs",
    "batch_size",
    "verbose",
    "ratios",
)

# Artifact bắt buộc của một out_dir LSTM (không thêm bớt âm thầm).
REQUIRED_LSTM_ARTIFACTS: tuple[str, ...] = (
    "model.keras",
    "scaler.json",
    "split_manifest.json",
    "manifest.json",
    "metrics.json",
)

# Contract feature/shape (khớp configs/model.yaml khối lstm; schema khóa shape).
LSTM_FEATURES: list[str] = ["distance_m", "delta_distance_m"]
LSTM_INPUT_SHAPE: list[int] = [10, 2]
LSTM_OUTPUT_SHAPE: list[int] = [5]

MANIFEST_SCHEMA_PATH = Path(__file__).resolve().parents[1] / (
    "schemas/model_manifest_v1.schema.json"
)

_SCHEMA_VALIDATOR: Draft202012Validator | None = None


def _iso_utc_ms(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _manifest_validator() -> Draft202012Validator:
    """Validator Draft 2020-12 cho ``model_manifest_v1.schema.json``.

    FormatChecker mặc định có thể thiếu rfc3339-validator trong .venv —
    ``created_at`` vẫn được kiểm TƯỜNG MINH qua ``parse_iso_utc`` trong
    ``validate_manifest_document``, không phụ thuộc plugin format.
    """
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        schema = json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
        _SCHEMA_VALIDATOR = Draft202012Validator(
            schema, format_checker=FormatChecker()
        )
    return _SCHEMA_VALIDATOR


def validate_manifest_document(manifest: Mapping[str, Any]) -> None:
    """Validate manifest qua schema + timestamp tự kiểm. Lỗi → ``ValueError``."""
    errors = sorted(_manifest_validator().iter_errors(dict(manifest)), key=str)
    if errors:
        details = "; ".join(e.message for e in errors[:5])
        raise ValueError(f"manifest không hợp lệ theo schema: {details}")
    try:
        created = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or created.utcoffset().total_seconds() != 0:
            raise ValueError("created_at phải có timezone UTC")
    except (KeyError, ValueError) as exc:
        raise ValueError(f"manifest created_at không parse được: {exc}") from exc


def validate_lstm_config(manifest: Mapping[str, Any], config: Mapping[str, Any]) -> None:
    """Artifact/config contract dùng chung evaluator và runtime."""
    window = WindowConfig.from_dict(config)
    lstm = config["lstm"]
    expected = {
        "model_type": "lstm", "input_shape": LSTM_INPUT_SHAPE,
        "output_shape": LSTM_OUTPUT_SHAPE, "features": LSTM_FEATURES, "units": "m",
        "window_size": window.window_size, "delta_s": window.delta_s,
        "horizons_ms": list(window.horizons_ms),
        "model_version": lstm["model_version"],
        "preprocessing_version": lstm["preprocessing_version"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"LSTM artifact {key} không khớp config")
    for key in ("window_size", "delta_s", "horizons_ms", "features", "input_shape", "output_shape"):
        if lstm[key] != expected[key]:
            raise ValueError(f"lstm.{key} lệch config dùng chung")


def _resolve_lstm_params(
    config: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """Gộp config YAML + override → tham số train đã validate.

    Key lạ → ``ValueError``. Giá trị sai kiểu/miền → ``ValueError``. Không
    default ẩn cho field kiến trúc thiếu trong khối ``lstm``.
    """
    unknown = sorted(set(overrides) - set(ALLOWED_LSTM_OVERRIDES))
    if unknown:
        raise ValueError(
            f"config_overrides có key không hỗ trợ: {unknown}; chỉ nhận "
            f"{list(ALLOWED_LSTM_OVERRIDES)}"
        )
    lstm = config.get("lstm")
    if not isinstance(lstm, Mapping):
        raise ValueError("config thiếu khối 'lstm' (bắt buộc cho train LSTM)")
    for key in (
        "model_version",
        "preprocessing_version",
        "features",
        "input_shape",
        "output_shape",
        "lstm_units",
        "dropout",
        "dense_units",
        "dense_activation",
        "batch_size",
        "max_epochs",
        "learning_rate",
        "loss",
        "optimizer",
        "metrics",
        "callbacks",
    ):
        if key not in lstm:
            raise ValueError(f"config lstm thiếu key bắt buộc: {key}")
    if list(lstm["features"]) != LSTM_FEATURES:
        raise ValueError(
            f"lstm.features {list(lstm['features'])} phải là {LSTM_FEATURES}"
        )
    if list(lstm["input_shape"]) != LSTM_INPUT_SHAPE:
        raise ValueError(
            f"lstm.input_shape {list(lstm['input_shape'])} phải là "
            f"{LSTM_INPUT_SHAPE}"
        )
    if list(lstm["output_shape"]) != LSTM_OUTPUT_SHAPE:
        raise ValueError(
            f"lstm.output_shape {list(lstm['output_shape'])} phải là "
            f"{LSTM_OUTPUT_SHAPE}"
        )
    if str(lstm["loss"]).lower() != "huber":
        raise ValueError(
            f"lstm.loss={lstm['loss']!r} — pipeline chỉ contract 'huber'"
        )
    if str(lstm["optimizer"]).lower() != "adam":
        raise ValueError(
            f"lstm.optimizer={lstm['optimizer']!r} — pipeline chỉ contract 'adam'"
        )

    if list(lstm["metrics"]) != ["mae"]:
        raise ValueError("lstm.metrics chỉ hỗ trợ [mae]")
    if lstm["preprocessing_version"] != PP_LSTM_VERSION:
        raise ValueError("lstm.preprocessing_version không khớp scaler")
    params: dict[str, Any] = {
        "model_version": str(lstm["model_version"]),
        "preprocessing_version": str(lstm["preprocessing_version"]),
        "lstm_units": int(lstm["lstm_units"]),
        "dropout": float(lstm["dropout"]),
        "dense_units": int(lstm["dense_units"]),
        "dense_activation": str(lstm["dense_activation"]),
        "batch_size": int(lstm["batch_size"]),
        "max_epochs": int(lstm["max_epochs"]),
        "learning_rate": float(lstm["learning_rate"]),
        "metrics": [str(m) for m in lstm["metrics"]],
        "callbacks": dict(lstm["callbacks"]),
        "seed": int(config["random_seed"]),
        "ratios": (0.7, 0.15, 0.15),
        "verbose": 1,
    }
    for key in ("lstm_units", "dense_units"):
        if params[key] < 1:
            raise ValueError(f"lstm.{key} phải >= 1, nhận {params[key]}")
    if not 0.0 <= params["dropout"] < 1.0:
        raise ValueError(f"lstm.dropout phải trong [0,1), nhận {params['dropout']}")
    if params["learning_rate"] <= 0:
        raise ValueError("lstm.learning_rate phải > 0")

    if "seed" in overrides:
        params["seed"] = int(overrides["seed"])
    if params["seed"] < 0:
        raise ValueError(f"seed phải >= 0, nhận {params['seed']}")
    if "max_epochs" in overrides:
        params["max_epochs"] = int(overrides["max_epochs"])
    if params["max_epochs"] < 1:
        raise ValueError(f"max_epochs phải >= 1, nhận {params['max_epochs']}")
    if "batch_size" in overrides:
        params["batch_size"] = int(overrides["batch_size"])
    if params["batch_size"] < 1:
        raise ValueError(f"batch_size phải >= 1, nhận {params['batch_size']}")
    if "verbose" in overrides:
        params["verbose"] = int(overrides["verbose"])
    if params["verbose"] not in (0, 1, 2):
        raise ValueError(f"verbose phải 0/1/2, nhận {params['verbose']}")
    if "ratios" in overrides:
        ratios = tuple(float(r) for r in overrides["ratios"])
        if len(ratios) != 3:
            raise ValueError(f"ratios phải 3 giá trị (train,val,test), nhận {ratios}")
        params["ratios"] = ratios
    ratios = params["ratios"]
    if (any(not math.isfinite(r) or r <= 0 for r in ratios)
            or not math.isclose(sum(ratios), 1.0)):
        raise ValueError("train/val/test ratios phải > 0, hữu hạn và tổng = 1")
    return params


def _import_tf():
    """Import TensorFlow LAZY — chỉ gọi khi thật sự build/train/nạp model."""
    try:
        import tensorflow as tf  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - AI env luôn có TF
        raise RuntimeError(
            "TensorFlow chưa cài — train/nạp LSTM cần environment AI riêng "
            "(tensorflow==2.20.0, xem PLAN.md mục 4); không cài vào iot_backend "
            ".venv"
        ) from exc
    return tf


def build_lstm_model(params: Mapping[str, Any]):
    """Dựng model Keras đúng contract: Input(10,2) → LSTM → Dropout →
    Dense(relu) → Dense(5); Adam; Huber. Seed TF được đặt trước build."""
    tf = _import_tf()
    seed = int(params["seed"])
    tf.keras.utils.set_random_seed(seed)
    try:  # determinism ops là optional — không có/c hardware không hỗ thì bỏ qua
        tf.config.experimental.enable_op_determinism()
    except (AttributeError, RuntimeError):  # pragma: no cover
        pass

    callbacks_cfg = params["callbacks"]
    if not isinstance(callbacks_cfg, Mapping) or not {
        "early_stopping",
        "reduce_lr",
        "model_checkpoint",
    } <= set(callbacks_cfg):
        raise ValueError(
            "lstm.callbacks cần đủ early_stopping/reduce_lr/model_checkpoint"
        )

    model = tf.keras.Sequential(
        [
            tf.keras.layers.Input(shape=tuple(LSTM_INPUT_SHAPE)),
            tf.keras.layers.LSTM(int(params["lstm_units"])),
            tf.keras.layers.Dropout(float(params["dropout"])),
            tf.keras.layers.Dense(
                int(params["dense_units"]),
                activation=str(params["dense_activation"]),
            ),
            tf.keras.layers.Dense(int(LSTM_OUTPUT_SHAPE[0])),
        ],
        name="lstm_v1",
    )
    model.compile(
        optimizer=tf.keras.optimizers.Adam(
            learning_rate=float(params["learning_rate"])
        ),
        loss=tf.keras.losses.Huber(),
        metrics=[tf.keras.metrics.MeanAbsoluteError(name="mae")],
    )
    return model, tf, callbacks_cfg


def _build_callbacks(tf, callbacks_cfg: Mapping[str, Any], stage_dir: Path) -> list:
    """Callbacks từ config. ModelCheckpoint ghi vào staging (artifact tạm),
    save_weights_only=False → file ``.keras`` hoàn chỉnh."""
    es = callbacks_cfg["early_stopping"]
    rl = callbacks_cfg["reduce_lr"]
    cp = callbacks_cfg["model_checkpoint"]
    checkpoint_path = stage_dir / "model.keras"  # đích artifact, nằm trong staging
    return [
        tf.keras.callbacks.EarlyStopping(
            monitor=str(es["monitor"]),
            mode=str(es["mode"]),
            patience=int(es["patience"]),
            restore_best_weights=bool(es["restore_best_weights"]),
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor=str(rl["monitor"]),
            mode=str(rl["mode"]),
            factor=float(rl["factor"]),
            patience=int(rl["patience"]),
            min_lr=float(rl["min_lr"]),
        ),
        tf.keras.callbacks.ModelCheckpoint(
            filepath=str(checkpoint_path),
            monitor=str(cp["monitor"]),
            mode=str(cp["mode"]),
            save_best_only=bool(cp["save_best_only"]),
            save_weights_only=False,
        ),
    ]


def _finite_or_raise(values: Mapping[str, Any], where: str) -> None:
    """Mọi số trong dict/list lồng nhau phải hữu hạn — NaN/inf → ValueError."""
    if isinstance(values, dict):
        for key, value in values.items():
            _finite_or_raise(value, f"{where}.{key}")
    elif isinstance(values, (list, tuple)):
        for index, value in enumerate(values):
            _finite_or_raise(value, f"{where}[{index}]")
    elif isinstance(values, (float, np.floating)) and not math.isfinite(
        float(values)
    ):
        raise ValueError(f"{where} không hữu hạn: {values}")


def _split_metrics(model, scaler: LSTMScaler, split, name: str) -> dict:
    """Metric một split: loss/mae scaled (evaluate) + MAE mét per horizon
    (predict → inverse_y → so với y mét). Predict không mutate model."""
    x_scaled = scaler.transform_X(split.X)
    y_scaled = scaler.transform_y(split.y)
    evaluate = model.evaluate(x_scaled, y_scaled, verbose=0)
    loss, mae = float(evaluate[0]), float(evaluate[1])
    prediction = model.predict(x_scaled, verbose=0)
    if prediction.shape != split.y.shape:
        raise ValueError(
            f"predict shape {prediction.shape} lệch y {split.y.shape} ở "
            f"split '{name}'"
        )
    prediction_m = scaler.inverse_y(prediction)
    mae_m_per_horizon = np.mean(np.abs(prediction_m - split.y), axis=0)
    metrics = {
        "n_windows": int(split.n_windows),
        "loss": loss,
        "mae": mae,
        "mae_m_per_horizon": [float(v) for v in mae_m_per_horizon],
        "mae_m_mean": float(np.mean(mae_m_per_horizon)),
    }
    _finite_or_raise(metrics, f"metrics.{name}")
    return metrics


def _write_metrics(metrics_doc: Mapping[str, Any], path: Path) -> None:
    payload = json.dumps(
        metrics_doc, ensure_ascii=False, indent=2, allow_nan=False
    )
    path.write_text(payload + "\n", encoding="utf-8")


def train_lstm(
    dataset_path: str | Path,
    config_path: str | Path = "configs/model.yaml",
    out_dir: str | Path = "ai_model/artifacts/lstm-v1",
    config_overrides: Mapping[str, Any] | None = None,
    split_manifest_path: str | Path | None = None,
) -> dict:
    """Train LSTM theo pipeline module docstring; trả summary dict.

    Tham số:
      - ``dataset_path``: file JSONL (``load_jsonl_dataset`` — session_label
        ở meta; M10 xử lý session, không tự tạo leakage path ở đây).
      - ``config_path``: YAML cấu hình (khối ``lstm`` + windowing).
      - ``out_dir``: thư mục ghi 5 artifact (bắt buộc — xem
        ``REQUIRED_LSTM_ARTIFACTS``). Ghi atomic-ish qua staging.
      - ``config_overrides``: ``seed``, ``max_epochs``, ``batch_size``,
        ``verbose``, ``ratios`` — key lạ → ``ValueError``.
      - ``split_manifest_path``: file chứa danh sách session train/val/test
        đã chốt. Khi truyền, danh sách này được dùng nguyên vẹn; không
        randomize lại theo seed.

    Lỗi dataset/split/scaler/fit xảy ra TRƯỚC hoặc TRONG staging → out_dir
    không bị ghi artifact dở (staging tự dọn).
    """
    overrides = dict(config_overrides or {})
    with Path(config_path).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    params = _resolve_lstm_params(config, overrides)
    window_config = WindowConfig.from_dict(config)

    records = load_jsonl_dataset(dataset_path)
    session_split = None
    if split_manifest_path is not None:
        split_path = Path(split_manifest_path)
        session_split = json.loads(split_path.read_text(encoding="utf-8"))
        if not isinstance(session_split, Mapping) or "sessions" not in session_split:
            raise ValueError("split manifest phải là object có khóa sessions")
        manifest_ratios = tuple(float(value) for value in session_split.get("ratios", params["ratios"]))
        if tuple(params["ratios"]) != manifest_ratios:
            raise ValueError(
                f"ratios config {params['ratios']} lệch split manifest {list(manifest_ratios)}"
            )
    dataset = build_lstm_dataset(
        records,
        window_config,
        model_config=config,
        seed=params["seed"],
        ratios=params["ratios"],
        session_split=session_split,
    )

    # Scaler fit CHỈ trên train (X và y) — chống leakage; transform mọi split
    # bằng tham số train-only.
    scaler = LSTMScaler.fit(dataset.train.X, dataset.train.y)

    # Build + fit. TensorFlow import lazy, chỉ lúc này.
    model, tf, callbacks_cfg = build_lstm_model(params)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stage_dir = out / f".lstm_stage_{os.getpid()}"
    stage_dir.mkdir(parents=True, exist_ok=False)
    try:
        callbacks = _build_callbacks(tf, callbacks_cfg, stage_dir)
        history = model.fit(
            scaler.transform_X(dataset.train.X),
            scaler.transform_y(dataset.train.y),
            validation_data=(
                scaler.transform_X(dataset.val.X),
                scaler.transform_y(dataset.val.y),
            ),
            epochs=params["max_epochs"],
            batch_size=params["batch_size"],
            verbose=params["verbose"],
            callbacks=callbacks,
        )
        history_dict = {
            key: [float(v) for v in values]
            for key, values in history.history.items()
        }
        _finite_or_raise(history_dict, "history")

        # Split manifest M10: không timestamp tự sinh → hash deterministic.
        split_manifest = dataset.split_manifest
        split_manifest_sha256 = write_split_manifest(
            split_manifest, stage_dir / "split_manifest.json"
        )

        # Model artifact = file checkpoint best (save_best_only, val_loss).
        checkpoint_file = stage_dir / "model.keras"
        if not checkpoint_file.is_file():
            raise RuntimeError(
                "ModelCheckpoint không ghi được model.keras trong staging — "
                "fit có chạy tối thiểu 1 epoch?"
            )
        model_file_sha256 = sha256_file(checkpoint_file)
        scaler_sha256 = write_scaler_json(
            scaler.to_dict(), stage_dir / "scaler.json"
        )

        manifest = {
            "schema_version": "1.0",
            "model_type": "lstm",
            "model_version": params["model_version"],
            "preprocessing_version": params["preprocessing_version"],
            "framework": {
                "name": "tensorflow",
                "version": str(tf.__version__),
                "keras_version": str(tf.keras.__version__),
            },
            "input_shape": list(LSTM_INPUT_SHAPE),
            "output_shape": list(LSTM_OUTPUT_SHAPE),
            "features": list(LSTM_FEATURES),
            "units": "m",
            "window_size": int(window_config.window_size),
            "delta_s": float(window_config.delta_s),
            "horizons_ms": [int(h) for h in window_config.horizons_ms],
            "random_seed": params["seed"],
            "model_file_sha256": model_file_sha256,
            "scaler_sha256": scaler_sha256,
            "dataset_sha256": sha256_file(dataset_path),
            "split_manifest_sha256": split_manifest_sha256,
            "n_train_windows": int(dataset.train.n_windows),
            "n_val_windows": int(dataset.val.n_windows),
            "n_test_windows": int(dataset.test.n_windows),
            "created_at": _iso_utc_ms(datetime.now(timezone.utc)),
        }
        # Validate TRƯỚC khi lộ artifact ra out_dir (schema + timestamp).
        validate_manifest_document(manifest)
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )

        # Metrics phải mô tả chính checkpoint được phát hành, kể cả khi
        # restore_best_weights bị operator tắt.
        model = tf.keras.models.load_model(checkpoint_file)
        # Metrics: test chỉ báo cáo SAU fit — không dùng cho callbacks/selection.
        metrics_doc = {
            "model_version": params["model_version"],
            "preprocessing_version": params["preprocessing_version"],
            "n_windows": {
                "train": int(dataset.train.n_windows),
                "val": int(dataset.val.n_windows),
                "test": int(dataset.test.n_windows),
            },
            "metrics": {
                name: _split_metrics(model, scaler, split, name)
                for name, split in dataset.splits.items()
            },
            "history": history_dict,
        }
        _write_metrics(metrics_doc, stage_dir / "metrics.json")
        manifest["metrics_sha256"] = sha256_file(stage_dir / "metrics.json")
        validate_manifest_document(manifest)
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

        # Promote staging → out_dir (manifest.json ghi CUỐI: presence của nó
        # báo out_dir hoàn chỉnh). os.replace = atomic trên cùng filesystem.
        for file_name in (
            "model.keras",
            "scaler.json",
            "split_manifest.json",
            "metrics.json",
            "manifest.json",
        ):
            source = stage_dir / file_name
            os.replace(source, out / file_name)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    return {
        "dataset": str(dataset_path),
        "config": str(config_path),
        "out_dir": str(out),
        "model_version": manifest["model_version"],
        "preprocessing_version": manifest["preprocessing_version"],
        "framework": manifest["framework"],
        "random_seed": params["seed"],
        "ratios": list(params["ratios"]),
        "n_windows": {
            "train": manifest["n_train_windows"],
            "val": manifest["n_val_windows"],
            "test": manifest["n_test_windows"],
        },
        "scaler_means": dict(scaler.means),
        "metrics": metrics_doc["metrics"],
        "history": {
            key: values[-1] for key, values in history_dict.items()
        },
        "artifacts": {
            name: str(out / name) for name in REQUIRED_LSTM_ARTIFACTS
        },
        "created_at": manifest["created_at"],
    }


def load_lstm_artifact(out_dir: str | Path) -> tuple[Any, LSTMScaler, dict]:
    """Nạp ``(model, scaler, manifest)`` từ artifact dir — ĐƯỜNG DẪN TIN CẬY.

    Kiểm (thiếu bất kỳ → exception, KHÔNG fallback âm thầm):
    - đủ 5 file ``REQUIRED_LSTM_ARTIFACTS`` (thiếu → ``FileNotFoundError``);
    - manifest hợp lệ schema + ``created_at`` parse được (``ValueError``);
    - contract: model_type lstm, preprocessing pp-lstm-v1, features/input/
      output shape đúng, units m (``ValueError``);
    - SHA-256 file model/scaler/split manifest khớp bytes thực trong dir
      (mismatch → ``ValueError``); split manifest hash khớp cả canonical
      JSON của dict đã đọc;
    - scaler.json round-trip ``LSTMScaler.from_dict`` thành công;
    - model Keras input ``(N,10,2)`` → output ``(N,5)``.
    """
    directory = Path(out_dir)
    missing = [
        name for name in REQUIRED_LSTM_ARTIFACTS
        if not (directory / name).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"artifact LSTM không đầy đủ trong {directory}: thiếu {missing}"
        )

    manifest = json.loads(
        (directory / "manifest.json").read_text(encoding="utf-8")
    )
    validate_manifest_document(manifest)

    if manifest.get("model_type") != "lstm":
        raise ValueError(
            f"model_type phải là 'lstm', nhận {manifest.get('model_type')!r}"
        )
    if manifest.get("preprocessing_version") != PP_LSTM_VERSION:
        raise ValueError(
            f"preprocessing_version phải là {PP_LSTM_VERSION}, nhận "
            f"{manifest.get('preprocessing_version')!r}"
        )
    if list(manifest.get("features") or []) != LSTM_FEATURES:
        raise ValueError(
            f"manifest features {manifest.get('features')} không khớp "
            f"{LSTM_FEATURES} — từ chối nạp"
        )
    if list(manifest.get("input_shape") or []) != LSTM_INPUT_SHAPE:
        raise ValueError(
            f"manifest input_shape {manifest.get('input_shape')} không khớp "
            f"{LSTM_INPUT_SHAPE}"
        )
    if list(manifest.get("output_shape") or []) != LSTM_OUTPUT_SHAPE:
        raise ValueError(
            f"manifest output_shape {manifest.get('output_shape')} không khớp "
            f"{LSTM_OUTPUT_SHAPE}"
        )
    if manifest.get("units") != "m":
        raise ValueError(f"units phải là 'm', nhận {manifest.get('units')!r}")

    checks = (
        ("model.keras", "model_file_sha256"),
        ("scaler.json", "scaler_sha256"),
        ("split_manifest.json", "split_manifest_sha256"),
    )
    for file_name, key in checks:
        actual = sha256_file(directory / file_name)
        expected = manifest.get(key)
        if actual != expected:
            raise ValueError(
                f"SHA-256 {file_name} không khớp manifest ({key}): file="
                f"{actual}, manifest={expected} — artifact hỏng hoặc bị thay"
            )

    split_manifest = json.loads(
        (directory / "split_manifest.json").read_text(encoding="utf-8")
    )
    if canonical_json(split_manifest).encode("utf-8") != (
        directory / "split_manifest.json"
    ).read_bytes():
        raise ValueError(
            "split_manifest.json không phải canonical JSON — hash file khớp "
            "nhưng nội dung lệch chuẩn ghi (write_split_manifest)"
        )

    metrics_path = directory / "metrics.json"
    if "metrics_sha256" in manifest and sha256_file(metrics_path) != manifest["metrics_sha256"]:
        raise ValueError("SHA-256 metrics.json không khớp manifest")
    metrics_doc = json.loads(metrics_path.read_text(encoding="utf-8"))
    _finite_or_raise(metrics_doc, "metrics")
    if metrics_doc.get("model_version") != manifest["model_version"]:
        raise ValueError("metrics model_version không khớp manifest")
    if metrics_doc.get("preprocessing_version") != manifest["preprocessing_version"]:
        raise ValueError("metrics preprocessing_version không khớp manifest")
    for name in SPLIT_NAMES:
        count = split_manifest["splits"][name]["n_windows"]
        if count != manifest.get(f"n_{name}_windows") or count != metrics_doc["n_windows"][name]:
            raise ValueError(f"n_windows {name} không khớp artifact")
    scaler = LSTMScaler.load(directory / "scaler.json")
    if scaler.n_samples_fit != manifest["n_train_windows"]:
        raise ValueError("scaler n_samples_fit không khớp train windows")

    tf = _import_tf()
    model = tf.keras.models.load_model(directory / "model.keras", compile=False)
    if list(model.input_shape[1:]) != LSTM_INPUT_SHAPE:
        raise ValueError(
            f"model input_shape {model.input_shape} không khớp "
            f"{LSTM_INPUT_SHAPE}"
        )
    if list(model.output_shape[1:]) != LSTM_OUTPUT_SHAPE:
        raise ValueError(
            f"model output_shape {model.output_shape} không khớp "
            f"{LSTM_OUTPUT_SHAPE}"
        )
    return model, scaler, manifest


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Train LSTM theo session split")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--config", default="configs/model.yaml")
    parser.add_argument("--out-dir", default="ai_model/artifacts/lstm-v1")
    parser.add_argument("--epochs", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--split-manifest", help="File danh sách session train/val/test đã chốt")
    args = parser.parse_args(argv)
    overrides = {"verbose": 0}
    for key, value in (("max_epochs", args.epochs), ("batch_size", args.batch_size), ("seed", args.seed)):
        if value is not None:
            overrides[key] = value
    summary = train_lstm(args.dataset, args.config, args.out_dir, overrides, args.split_manifest)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
