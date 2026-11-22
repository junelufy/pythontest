#!/usr/bin/env bash
# Run in WSL #2. Does not download or modify raw L2 source.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="data/lighter_fresh_20261122"
CAL="$DATA/CALIBRATION_12D_TWO_SIDED/zone_12d_summary.json"
ROOT="$DATA/FORWARD_TP2_TP3_15D"
MAIN="$ROOT/paired_age120_skew120"
STRICT="$ROOT/paired_age120_skew10"
WINDOWS="/mnt/c/Users/June-ASUS/Downloads"
ZIP="$WINDOWS/LIGHTER_FORWARD_TP2_VS_TP3_15D.zip"

if [ ! -s "$CAL" ]; then
  echo "Missing 12-day frozen calibration summary: $CAL" >&2
  echo "Run 'bash scripts/run_lighter_12d_calibration_wsl.sh' first." >&2
  exit 2
fi
if [ ! -d "$DATA/book/XAU" ] || [ ! -d "$DATA/book/PAXG" ]; then
  echo "Missing local XAU/PAXG raw historical L2 book snapshots." >&2
  exit 2
fi

echo "=== Unit tests: long/short grids; TP 2 vs 3 USD per oz; maker/taker; four-leg fees; MTM ==="
python3 -m pytest -q tests/test_lighter_forward_15d_grid.py tests/test_lighter_12d_zones.py tests/test_lighter_spread_feasibility.py

echo "=== Primary historical forward window: Nov 04 01:00 - Nov 19 00:00 UTC (exclusive) ==="
python3 -m scripts.forwardtest_lighter_spread_grid_15d \
  --data "$DATA" --zones "$CAL" --output "$MAIN" \
  --quantities 0.5 1.0 --tp 2 3 \
  --max-quote-age-s 120 --max-snapshot-skew-s 120

echo "=== Conservative timestamp-synchronization sensitivity: maximum 10 seconds ==="
python3 -m scripts.forwardtest_lighter_spread_grid_15d \
  --data "$DATA" --zones "$CAL" --output "$STRICT" \
  --quantities 0.5 1.0 --tp 2 3 \
  --max-quote-age-s 120 --max-snapshot-skew-s 10

mkdir -p "$WINDOWS"
python3 -m zipfile -c "$ZIP" \
  "$MAIN/forward_15d_summary.json" \
  "$MAIN/tp_2_vs_3_scenarios.csv" \
  "$MAIN/trades_all_scenarios.csv" \
  "$MAIN/terminal_open_positions.csv" \
  "$STRICT/forward_15d_summary.json" \
  "$STRICT/tp_2_vs_3_scenarios.csv" \
  "$STRICT/trades_all_scenarios.csv" \
  "$STRICT/terminal_open_positions.csv"

echo "DONE. Please upload:"
echo "C:\Users\June-ASUS\Downloads\LIGHTER_FORWARD_TP2_VS_TP3_15D.zip"
echo "No API credits used. Historical snapshot maker fills remain hypothetical."
