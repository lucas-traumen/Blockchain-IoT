#!/usr/bin/env python3
"""Render the fixed HRC session reward policy and a local smoke balance trace."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    smoke = json.loads(args.smoke.read_text(encoding="utf-8"))
    policy = smoke["policy"]
    balance = 20.0
    labels = ["Ban đầu", "Đạt\n+3", "Giới hạn\nngày", "Vi phạm\n−2", "Lỗi cảm biến\n0"]
    balances = [balance]
    balances += [balance := balance + 3.0]
    balances += [balance]
    balances += [balance := balance - 2.0]
    balances += [balance]

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(figsize=(11, 6))
    colors = ["#64748b", "#16a34a", "#94a3b8", "#dc2626", "#2563eb"]
    bars = ax.bar(labels, balances, color=colors, width=0.65)
    ax.bar_label(bars, labels=[f"{value:.0f} HRC" for value in balances], padding=4)
    ax.set_ylim(0, max(balances) * 1.25)
    ax.set_ylabel("Số dư beneficiary (HRC)")
    ax.set_title("HRC session reward policy — local smoke trace")
    ax.text(
        0.02,
        0.02,
        f"stake={policy['stake']} | reward={policy['reward']} | penalty={policy['penalty']} | pool={policy['rewardPool']}\n"
        "Một lần thưởng/ví/ngày; SENSOR_FAULT không cộng/trừ.",
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        color="#475569",
        fontsize=10,
    )
    fig.tight_layout(rect=(0, 0.09, 1, 1))
    fig.savefig(out / "reward-policy.png", dpi=300, facecolor="white")
    fig.savefig(out / "reward-policy.svg", facecolor="white")
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
