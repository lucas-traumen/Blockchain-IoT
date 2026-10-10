"""Evaluate the LSTM artifact at every forecast horizon."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from ai_model.evaluate import evaluate_lstm_models


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--config", default="configs/model.yaml")
    parser.add_argument("--sp", type=float, default=0.04)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    horizons = [int(value) for value in config["horizons_ms"]]
    reports = {}
    for index, horizon in enumerate(horizons):
        reports[str(horizon)] = evaluate_lstm_models(
            args.dataset,
            args.model_dir,
            config_path=args.config,
            sp_m=args.sp,
            horizon_index=index,
        )

    payload = {
        "dataset": str(args.dataset),
        "model_dir": str(args.model_dir),
        "sp_m": args.sp,
        "horizons_ms": horizons,
        "reports_by_horizon_ms": reports,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "horizons_ms": horizons}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
