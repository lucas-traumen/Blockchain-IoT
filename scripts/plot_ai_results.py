"""Create LSTM evaluation figures with explicit real/synthetic provenance."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.font_manager import FontProperties
from matplotlib.ticker import MaxNLocator

MODELS = ("lstm", "persistence", "constant_velocity")
LABELS = {"lstm": "LSTM", "persistence": "Persistence", "constant_velocity": "Constant velocity"}
COLORS = {"lstm": "#2563eb", "persistence": "#059669", "constant_velocity": "#ea580c"}
MARKERS = {"lstm": "o", "persistence": "s", "constant_velocity": "^"}


def read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_predictions(path: str | Path) -> dict[str, list[dict[str, str]]]:
    grouped: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            grouped[row["session_id"]].append(row)
    return {sid: sorted(rows, key=lambda row: int(row["sequence_last"])) for sid, rows in sorted(grouped.items())}


def wrap_footer(fig, text: str, fontsize: int = 9) -> str:
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    font = FontProperties(family="DejaVu Sans", size=fontsize)
    max_width = fig.bbox.width * 0.95
    lines: list[str] = []
    for paragraph in text.splitlines():
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            width = renderer.get_text_width_height_descent(candidate, font, False)[0]
            if line and width > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return "\n".join(lines)


def unavailable_panel(ax, title: str, detail: str, *, fontsize: int = 18) -> None:
    ax.set_axis_off()
    ax.text(.5, .62, title, transform=ax.transAxes, ha="center", va="center", fontsize=fontsize, weight="bold", color="#334155")
    ax.text(.5, .38, detail, transform=ax.transAxes, ha="center", va="center", fontsize=12, color="#475569", linespacing=1.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--all-horizons", required=True)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--debounced", help="Optional replay metrics from EventDetector 2/3")
    args = parser.parse_args(argv)

    report = read_json(args.report)
    all_reports = read_json(args.all_horizons)["reports_by_horizon_ms"]
    training = read_json(args.metrics)
    predictions = read_predictions(args.predictions)
    rows = [json.loads(line) for line in Path(args.dataset).read_text(encoding="utf-8").splitlines() if line.strip()]
    source_flags = {row.get("synthetic") for row in rows}
    if source_flags not in ({True}, {False}):
        raise ValueError("Figure dataset needs one explicit real/synthetic source")
    synthetic = source_flags == {True}
    if report.get("synthetic") is not synthetic or any(
        item.get("synthetic") is not synthetic for item in all_reports.values()
    ):
        raise ValueError("Report and figure dataset source differ")
    source_caption = "SYNTHETIC — KẾT QUẢ MÔ PHỎNG" if synthetic else "REAL — DỮ LIỆU ĐO"
    summary_path = Path(args.dataset).with_suffix(".summary.json")
    summary = read_json(summary_path) if summary_path.is_file() else {}
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.spines.top": False, "axes.spines.right": False, "axes.titleweight": "bold", "figure.facecolor": "white"})

    horizons = [int(value) for value in report["horizons_ms"]]
    sp_mm = float(report["sp_m"]) * 1000.0
    event_count = int(report["models"]["lstm"]["event_metrics"]["n_events"])
    scope = f"LSTM only | test: {len(report['test_sessions'])} session / {report['n_test_windows']:,} cửa sổ | Sp = {sp_mm:.0f} mm"
    if event_count:
        note = f"Test có {event_count} crossing downward tại Sp; F1 tính theo episode cảnh báo và event, không phải accuracy từng mẫu."
    else:
        note = "Test không có crossing tại Sp: precision / recall / F1 = N/A; đây là thiếu support sự kiện, không phải điểm 0."
    exported: list[str] = []
    layout_checks: list[dict] = []

    def save(fig, name: str, *, footnote: str = scope) -> None:
        if synthetic:
            footnote = source_caption + " | Không phải phép đo ESP32/HC-SR04.\n" + footnote
        wrapped = wrap_footer(fig, footnote)
        footer = fig.text(.025, .025, wrapped, fontsize=9, color="#475569", va="bottom", linespacing=1.4)
        fig.canvas.draw()
        footer_box = footer.get_window_extent(fig.canvas.get_renderer())
        bottom = footer_box.y1 / fig.bbox.height + .04
        fig.tight_layout(rect=(0, bottom, 1, .94), pad=1.4)
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        axes_boxes = [ax.get_tightbbox(renderer) for ax in fig.axes if ax.get_visible()]
        gap = min((box.y0 - footer_box.y1 for box in axes_boxes), default=1.0)
        if gap < 0 or footer_box.x1 > fig.bbox.width:
            raise RuntimeError(f"{name}: footer overlaps plot or exceeds image width")
        layout_checks.append({"figure": name, "footer_lines": len(wrapped.splitlines()), "plot_footer_gap_px": round(gap, 2)})
        for ext in ("png", "svg"):
            fig.savefig(out / f"{name}.{ext}", dpi=300, facecolor="white")
            exported.append(f"{name}.{ext}")
        pdf.savefig(fig)
        plt.close(fig)

    with PdfPages(out / "figures.pdf") as pdf:
        scenarios = sorted(Counter(str(row.get("scenario", "unknown")) for row in rows))
        sample_counts = Counter(str(row.get("scenario", "unknown")) for row in rows)
        session_counts = Counter(scenario for scenario, _ in {
            (str(row.get("scenario", "unknown")), row["session_label"])
            for row in rows if row.get("session_label")
        })
        fig, axes = plt.subplots(1, 2, figsize=(12, 5.5))
        for ax, values, ylabel in ((axes[0], [sample_counts[s] for s in scenarios], "Số mẫu raw"), (axes[1], [session_counts[s] for s in scenarios], "Số session")):
            bars = ax.bar(scenarios, values, color="#2563eb")
            ax.bar_label(bars, padding=3)
            ax.set_ylabel(ylabel)
            ax.set_ylim(0, max(values, default=1) * 1.22)
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.tick_params(axis="x", labelrotation=25)
        fig.suptitle(f"Dataset LSTM {'SYNTHETIC' if synthetic else 'REAL'}: {len(rows):,} mẫu / {len(set(row.get('session_label') for row in rows))} session", fontsize=16)
        save(fig, "dataset_overview", footnote=scope + "\nĐếm mẫu và session riêng; mỗi session thuộc đúng một split trước windowing.")

        fig, ax = plt.subplots(figsize=(12, 5.3))
        valid = [float(row["distance_mm"]) for row in rows if row.get("valid") is True and row.get("distance_mm") is not None]
        if valid:
            bins = np.arange(0, max(100, max(valid) + 20), 20)
            ax.hist(valid, bins=bins, color="#2563eb", alpha=.8)
        ax.axvline(sp_mm, color="#dc2626", linestyle="--", label=f"Sp = {sp_mm:.0f} mm")
        ax.set(xlabel="Khoảng cách đo (mm)", ylabel="Số mẫu (thang log)", yscale="log")
        ax.legend()
        fig.suptitle("Phân bố khoảng cách trong dataset đang đánh giá", fontsize=16)
        save(fig, "distance_distribution", footnote=scope)

        history = training.get("history", {})
        fig, axes = plt.subplots(1, 2, figsize=(12, 5.7))
        epochs = np.arange(1, len(history.get("loss", [])) + 1)
        for ax, keys, ylabel in ((axes[0], ("loss", "val_loss"), "Huber loss (scaled)"), (axes[1], ("mae", "val_mae"), "MAE (scaled)")):
            for key, label, color in ((keys[0], "Train", "#2563eb"), (keys[1], "Validation", "#ea580c")):
                if key in history:
                    ax.plot(epochs, history[key], label=label, color=color)
            ax.set(xlabel="Epoch", ylabel=ylabel)
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            ax.grid(alpha=.2)
            ax.legend()
        fig.suptitle("Đường học LSTM", fontsize=16)
        save(fig, "training_history", footnote=scope + "\nScaler fit chỉ trên train; test không tham gia chọn checkpoint.")

        fig, axes = plt.subplots(1, 2, figsize=(12, 5.7))
        for ax, field, title in ((axes[0], "mae_per_horizon", "MAE"), (axes[1], "rmse_per_horizon", "RMSE")):
            for name in MODELS:
                metric = report["models"][name]["regression"]
                values = np.asarray(metric[field], dtype=float) * 1000
                ax.plot(horizons, values, marker=MARKERS[name], color=COLORS[name], label=LABELS[name], linewidth=2)
            ax.set(xlabel="Horizon dự báo (ms)", ylabel=f"{title} (mm)", xticks=horizons)
            ax.set_ylim(bottom=0)
            ax.grid(alpha=.2)
            ax.legend()
        fig.suptitle("Sai số dự báo trên tập test", fontsize=16)
        save(fig, "regression_errors")

        fields = ("precision", "recall", "f1", "false_warning_rate", "miss_rate")
        ticks = ("Precision", "Recall", "F1-score", "False-warning rate", "Miss rate")
        fig, ax = plt.subplots(figsize=(12, 5.8))
        if event_count == 0:
            unavailable_panel(ax, "Precision / Recall / F1 = N/A", "Tập test có 0 lần vượt ngưỡng Sp.\nMAE và RMSE của dự báo khoảng cách vẫn tính được.")
        else:
            x = np.arange(len(fields))
            width = .8 / len(MODELS)
            for i, name in enumerate(MODELS):
                metrics = report["models"][name]["event_metrics"]
                values = [metrics[field] for field in fields]
                positions = x - .4 + width / 2 + i * width
                bars = ax.bar(positions, [np.nan if value is None else value for value in values], width=width, label=LABELS[name], color=COLORS[name])
                for bar, value in zip(bars, values):
                    if value is None:
                        ax.text(bar.get_x() + bar.get_width() / 2, .04, "N/A", ha="center", rotation=90, color=COLORS[name], fontsize=8)
            ax.set(xticks=x, xticklabels=ticks, ylim=(0, 1.25), ylabel="Giá trị metric")
            ax.legend(loc="upper center", ncols=3)
            ax.grid(axis="y", alpha=.2)
        fig.suptitle(f"Metric cảnh báo offline, trước debounce — {event_count} event test", fontsize=16)
        save(fig, "event_metrics_500ms", footnote=scope + "\n" + note)

        fig, axes = plt.subplots(1, len(MODELS), figsize=(15, 5.8), squeeze=False)
        for ax, name in zip(axes[0], MODELS):
            values = [all_reports[str(h)]["models"][name]["event_metrics"]["f1"] for h in horizons]
            ax.set_title(LABELS[name], color=COLORS[name], fontsize=13)
            if all(value is None for value in values):
                unavailable_panel(ax, "F1 = N/A", "Không có support sự kiện.")
                continue
            ax.plot(horizons, [np.nan if value is None else value for value in values], color=COLORS[name], marker=MARKERS[name], linewidth=2)
            ax.set(xlabel="Horizon (ms)", ylabel="F1 offline", xticks=horizons, ylim=(0, 1.2))
            ax.grid(alpha=.2)
        fig.suptitle("F1-score LSTM và baseline theo horizon", fontsize=16)
        save(fig, "f1_by_horizon", footnote=scope + "\n" + note)

        fig, ax = plt.subplots(figsize=(12, 5.4))
        if event_count == 0:
            unavailable_panel(ax, "Test: 0 crossing", "Chưa có sự kiện để kết luận model cảnh báo đúng hoặc bỏ sót.")
        else:
            x = np.arange(len(MODELS))
            width = .8 / 3
            for i, (label, values) in enumerate((("TP episode", []), ("FP episode", []), ("FN event", []))):
                values = []
                for name in MODELS:
                    metrics = report["models"][name]["event_metrics"]
                    values.append(metrics["n_tp_episodes"] if label == "TP episode" else metrics["n_episodes"] - metrics["n_tp_episodes"] if label == "FP episode" else metrics["n_events"] - metrics["n_matched_events"])
                bars = ax.bar(x - .4 + width / 2 + i * width, values, width, label=label, color=("#059669", "#ea580c", "#dc2626")[i])
                ax.bar_label(bars, padding=3)
            ax.set(xticks=x, xticklabels=[LABELS[name] for name in MODELS], ylabel="Số lượng", ylim=(0, max(4, event_count + 1.5)))
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.legend(loc="upper center", ncols=3)
            ax.grid(axis="y", alpha=.2)
        fig.suptitle(f"Kết quả phát hiện trước debounce — {event_count} event", fontsize=16)
        save(fig, "event_outcomes", footnote=scope + "\nTN không được định nghĩa trong cách đếm event.")

        if args.debounced:
            confirmed = read_json(args.debounced)
            if confirmed.get("synthetic") is not synthetic:
                raise ValueError("Debounced report source differs")
            raw = report["models"]["lstm"]["event_metrics"]
            debounced = confirmed["metrics"]
            if debounced["n_events"] != raw["n_events"]:
                raise ValueError("Debounced event support differs")
            fig, ax = plt.subplots(figsize=(12, 5.8))
            fields = ("precision", "recall", "f1", "miss_rate")
            x = np.arange(len(fields))
            for shift, metrics, label, color in ((-.18, raw, "LSTM trước debounce", "#2563eb"),
                                                  (.18, debounced, "LSTM sau xác nhận 2/3", "#ea580c")):
                values = [np.nan if metrics[field] is None else metrics[field] for field in fields]
                bars = ax.bar(x + shift, values, .36, label=label, color=color)
                ax.bar_label(bars, fmt="%.3f", padding=3)
            ax.set(xticks=x, xticklabels=("Precision", "Recall", "F1-score", "Miss rate"),
                   ylim=(0, 1.2), ylabel="Giá trị metric")
            ax.legend(loc="upper center", ncols=2)
            ax.grid(axis="y", alpha=.2)
            fig.suptitle("Ảnh hưởng của xác nhận cảnh báo 2/3 — LSTM, horizon 500 ms", fontsize=16)
            save(fig, "warning_confirmation", footnote=scope + "\nSau debounce dùng thời điểm xác nhận thực tế trong replay; chưa mô phỏng độ trễ trên board/MQTT.")

        chosen_streams = list(predictions.items())[:6]
        fig, axes = plt.subplots(len(chosen_streams), 1, figsize=(12, max(4, 3.4 * len(chosen_streams))), squeeze=False)
        for ax, (sid, stream) in zip(axes[:, 0], chosen_streams):
            time = np.asarray([float(row["time_s"]) for row in stream])
            ax.plot(time, [float(row["target_500ms_m"]) * 1000 for row in stream], label="Mô phỏng t + 500 ms" if synthetic else "Đo thực tế t + 500 ms", color="#111827", linewidth=1.2)
            for name in MODELS:
                ax.plot(time, [float(row[f"{name}_500ms_m"]) * 1000 for row in stream], label=LABELS[name], color=COLORS[name], linewidth=1, alpha=.8)
            ax.axhline(sp_mm, color="#dc2626", linestyle="--", label=f"Sp = {sp_mm:.0f} mm")
            ax.set(title=f"{stream[0]['scenario']} — {sid}", xlabel="Thời gian trong session (s)", ylabel="Khoảng cách (mm)")
            ax.grid(alpha=.2)
            ax.legend(ncols=4, fontsize=8)
        fig.suptitle("Ví dụ dự báo LSTM trên các session test", fontsize=16)
        save(fig, "prediction_examples")

        fig, ax = plt.subplots(figsize=(12, 5.5))
        ax.axis("off")
        cells = []
        for name in MODELS:
            regression = report["models"][name]["regression"]
            event = report["models"][name]["event_metrics"]
            fmt = lambda value: "N/A" if value is None else f"{value:.3f}"
            cells.append([LABELS[name], f"{regression['mae_mean'] * 1000:.2f}", f"{regression['rmse_mean'] * 1000:.2f}", fmt(event["precision"]), fmt(event["recall"]), fmt(event["f1"]), str(event["n_events"])])
        table = ax.table(cellText=cells, colLabels=["Model", "MAE TB (mm)", "RMSE TB (mm)", "Precision", "Recall", "F1 offline", "Events"], cellLoc="center", loc="center", colWidths=[.25, .14, .14, .12, .11, .12, .10])
        table.auto_set_font_size(False)
        table.set_fontsize(11)
        table.scale(1, 2.2)
        for (r, c), cell in table.get_celld().items():
            cell.set_edgecolor("white")
            if r == 0:
                cell.set_facecolor("#1e293b")
                cell.get_text().set_color("white")
                cell.get_text().set_weight("bold")
            else:
                cell.set_facecolor("#eff6ff" if r % 2 else "#f1f5f9")
        fig.suptitle("Bảng kết quả LSTM-only", fontsize=16)
        save(fig, "results_table", footnote=scope + "\nMAE/RMSE TB = trung bình 5 horizon; " + note)

    print(json.dumps({"output_dir": str(out), "files": exported + ["figures.pdf"], "matplotlib": matplotlib.__version__, "layout_checks": layout_checks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
