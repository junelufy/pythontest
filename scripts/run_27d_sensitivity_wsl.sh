#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="data/lighter_fresh_20261122"
OUT="$DATA/SPREAD_SENSITIVITY_27D"
WIN="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_SPREAD_SENSITIVITY_27D.zip"

echo "Running offline regression tests..."
python3 -m pytest -q tests/test_lighter_spread_feasibility.py

echo "Running NEW exploratory lower-threshold sensitivity study on existing 27-day L2 data. NO API calls."
python3 scripts/analyze_lighter_spread_feasibility.py \
  --input "$DATA" --output "$OUT" \
  --start 2026-10-23T01:00:00Z \
  --end 2026-11-18T23:59:00Z \
  --max-quote-age 120 --step-minutes 1 \
  --quantities 0.1 0.5 1.0 \
  --grids 0 5 10 15 18 20 22 24 \
  --compressions 2 5 \
  --max-holds 60 240 \
  --taker-bps 0 2 --maker-bps 0

python3 -m zipfile -c "$WIN" "$OUT/summary.json" "$OUT/spread_scenarios.csv"
echo "Upload this report: C:\\Users\\June-ASUS\\Downloads\\LIGHTER_SPREAD_SENSITIVITY_27D.zip"
