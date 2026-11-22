#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="data/lighter_fresh_20261122"
OUT="$DATA/SPREAD_FEASIBILITY"
WIN="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_SPREAD_FEASIBILITY_27D.zip"

if [ ! -d "$DATA/book/XAU" ] || [ ! -d "$DATA/book/PAXG" ]; then
  echo "Historical XAU/PAXG L2 files not found under $DATA" >&2
  exit 2
fi

echo "Running offline L2 backtest feasibility checks..."
python3 -m pytest -q tests/test_lighter_spread_feasibility.py

echo "Scanning 27-day synchronized Lighter Level-2 data; NO API credits needed."
python3 scripts/analyze_lighter_spread_feasibility.py \
  --input "$DATA" --output "$OUT" \
  --start 2026-10-23T01:00:00Z \
  --end 2026-11-18T23:59:00Z \
  --max-quote-age 120 --step-minutes 1 \
  --quantities 0.1 0.5 1.0 \
  --grids 25 27 29 31 \
  --compressions 2 5 \
  --max-holds 60 240 \
  --taker-bps 0 2 --maker-bps 0

echo "Packaging small summary (not raw data)..."
python3 -m zipfile -c "$WIN" "$OUT/summary.json" "$OUT/spread_scenarios.csv"
echo "Upload report: C:\\Users\\June-ASUS\\Downloads\\LIGHTER_SPREAD_FEASIBILITY_27D.zip"
