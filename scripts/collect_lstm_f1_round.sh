#!/usr/bin/env bash
set -euo pipefail

# Thu một đợt dữ liệu thật cho LSTM với Sp = 40 mm.
# Không dùng --non-interactive: mỗi session cần người vận hành thực hiện động tác.

mode="${1:-movement}"
run_dir="${2:-}"
api_url="${HRC_API_URL:-http://127.0.0.1:8001}"
device_id="${DEVICE_ID:-esp32_01}"

case "$mode" in
  movement)
    plan="configs/collection-lstm-f1-40mm.yaml"
    default_dir="data/datasets/real-20261010/f1-movement-01"
    ;;
  faults)
    plan="configs/collection-lstm-sensor-faults-40mm.yaml"
    default_dir="data/datasets/real-20261010/sensor-fault-01"
    ;;
  -h|--help|help)
    cat <<'USAGE'
Usage:
  scripts/collect_lstm_f1_round.sh movement [output-dir]
  scripts/collect_lstm_f1_round.sh faults [output-dir]

Environment:
  HRC_API_URL  API iot_backend, mặc định http://127.0.0.1:8001
  DEVICE_ID    device ESP32, mặc định esp32_01

Ví dụ:
  scripts/collect_lstm_f1_round.sh movement
  scripts/collect_lstm_f1_round.sh faults data/datasets/real-20261010/sensor-fault-02
USAGE
    exit 0
    ;;
  *)
    echo "Lỗi: mode phải là movement hoặc faults" >&2
    exit 2
    ;;
esac

if [[ -n "$run_dir" ]]; then
  output_dir="$run_dir"
else
  output_dir="$default_dir"
fi

if [[ -e "$output_dir" ]]; then
  echo "Lỗi: thư mục $output_dir đã tồn tại; chọn thư mục mới." >&2
  exit 1
fi

if ! curl --fail --silent --show-error "$api_url/health" >/dev/null; then
  echo "Lỗi: không truy cập được iot_backend tại $api_url" >&2
  exit 1
fi

echo "API: $api_url"
echo "Device: $device_id"
echo "Plan: $plan"
echo "Output: $output_dir"
echo "Mỗi session sẽ dừng tự động sau thời lượng trong plan."

exec .venv/bin/python -m scripts.collect_dataset \
  --api-url "$api_url" \
  --device-id "$device_id" \
  --plan "$plan" \
  --output-dir "$output_dir"
