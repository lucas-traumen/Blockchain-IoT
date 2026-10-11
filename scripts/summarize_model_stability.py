"""Summarize frozen-test metrics from multiple LSTM seed reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if len(reports) < 3:
        raise ValueError("at least three seed reports are required")
    horizons = reports[0]["horizons_ms"]
    output: dict[str, Any] = {"seed_count": len(reports), "horizons_ms": horizons, "models": {}}
    for model in ("lstm", "persistence", "constant_velocity"):
        model_output = {}
        for index, horizon in enumerate(horizons):
            values = []
            f1_values = []
            for report in reports:
                entry = report["reports_by_horizon_ms"][str(horizon)]["models"][model]
                values.append(float(entry["regression"]["mae_per_horizon"][index]))
                f1_values.append(float(entry["event_metrics"]["f1"]))
            model_output[str(horizon)] = {
                "mae_mean_m": {"mean": mean(values), "std": pstdev(values), "values": values},
                "event_f1": {"mean": mean(f1_values), "std": pstdev(f1_values), "values": f1_values},
            }
        output["models"][model] = model_output
    candidate = output["models"]["lstm"]
    baseline = output["models"]["constant_velocity"]
    output["decision"] = {
        "status": "REJECTED_SYNTHETIC_GATE",
        "candidate": "lstm-v2-synthetic",
        "active_model": "lstm-v1",
        "reason": "LSTM MAE mean is higher than constant_velocity at every frozen horizon across the seed summary",
        "mae_candidate_mean_m_at_500ms": candidate["500"]["mae_mean_m"]["mean"],
        "mae_baseline_mean_m_at_500ms": baseline["500"]["mae_mean_m"]["mean"],
    }
    if not all(
        candidate[str(horizon)]["mae_mean_m"]["mean"]
        > baseline[str(horizon)]["mae_mean_m"]["mean"]
        for horizon in horizons
    ):
        output["decision"]["reason"] = "Candidate/baseline comparison requires review; this summary does not auto-promote"
    json.dumps(output, allow_nan=False)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.reports]
    result = summarize(reports)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": result["decision"]["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
