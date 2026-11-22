#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="data/lighter_fresh_20261122"
OUT="$DATA/CALIBRATION_5D"
DEST="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_SPREAD_CALIBRATION_FIRST_5_DAYS.zip"

echo "=== First 5 days: calibration spread and distribution (NOT trade simulation) ==="
echo "Training 2026-10-23 01:00 UTC .. 2026-10-28 01:00 UTC, end exclusive"
echo "Holdout starts 2026-10-28 01:00 UTC. Do not select grids using heldout results."
python3 -m pytest -q tests/test_lighter_5d_calibration.py tests/test_lighter_spread_feasibility.py

python3 scripts/calibrate_lighter_spread_5d.py \
  --input "$DATA" \
  --output "$OUT" \
  --start 2026-10-23T01:00:00Z \
  --calibration-days 5 \
  --max-quote-age 120 \
  --step-minutes 1 \
  --quantities 0.1 0.5 1.0

python3 scripts/plot_lighter_5d_calibration.py \
  --summary "$OUT/calibration_5d_summary.json" \
  --samples "$OUT/calibration_5d_spread_samples.csv" \
  --output "$OUT/calibration_5d_spread_chart.html"

python3 -m zipfile -c "$DEST" \
  "$OUT/calibration_5d_summary.json" \
  "$OUT/calibration_5d_spread_samples.csv" \
  "$OUT/calibration_5d_spread_chart.html"

echo "READY: C:\\Users\\June-ASUS\\Downloads\\LIGHTER_SPREAD_CALIBRATION_FIRST_5_DAYS.zip"
echo "No API requests; did not execute or optimize on 22-day holdout"
