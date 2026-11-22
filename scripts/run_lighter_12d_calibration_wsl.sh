#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="data/lighter_fresh_20261122"
OUT="$DATA/CALIBRATION_12D_TWO_SIDED"
DEST="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_SPREAD_CALIBRATION_12D.zip"

echo "=== 12-day calibration: LONG low-spread + SHORT high-spread zones ==="
echo "TRAINING: 2026-10-23 01:00 to 2026-11-04 01:00 UTC (end exclusive)"
echo "HOLDOUT: 2026-11-04 01:00 to 2026-11-19 00:00 UTC (NOT READ)"

python3 -m pytest -q tests/test_lighter_5d_calibration.py tests/test_lighter_12d_zones.py tests/test_lighter_spread_feasibility.py

python3 -m scripts.calibrate_lighter_spread_5d \
  --input "$DATA" \
  --output "$OUT" \
  --start 2026-10-23T01:00:00Z \
  --calibration-days 12 \
  --max-quote-age 120 \
  --step-minutes 1 \
  --quantities 0.1 0.5 1.0

python3 -m scripts.summarize_lighter_12d_zones \
  --summary "$OUT/calibration_12d_summary.json" \
  --samples "$OUT/calibration_12d_spread_samples.csv" \
  --output "$OUT"

python3 -m scripts.plot_lighter_5d_calibration \
  --summary "$OUT/calibration_12d_summary.json" \
  --samples "$OUT/calibration_12d_spread_samples.csv" \
  --output "$OUT/calibration_12d_spread_chart.html"

mkdir -p /mnt/c/Users/June-ASUS/Downloads
python3 -m zipfile -c "$DEST" \
  "$OUT/calibration_12d_summary.json" \
  "$OUT/zone_12d_summary.json" \
  "$OUT/zone_12d_daily.csv" \
  "$OUT/calibration_12d_spread_samples.csv" \
  "$OUT/calibration_12d_spread_chart.html"

echo "READY: C:\\Users\\June-ASUS\\Downloads\\LIGHTER_SPREAD_CALIBRATION_12D.zip"
echo "NO API calls and NO 15-day future holdout reads; calibration only."
