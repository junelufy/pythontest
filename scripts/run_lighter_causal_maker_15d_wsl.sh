#!/usr/bin/env bash
# WSL 2: only local historical L2/public tape. No API requests.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="data/lighter_fresh_20261122"
CAL="$DATA/CALIBRATION_12D_TWO_SIDED/zone_12d_summary.json"
OUT="$DATA/CAUSAL_MAKER_FIRST_15D"
DEST="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_CAUSAL_MAKER_TP2_TP3_15D.zip"

if [ ! -s "$CAL" ]; then
  echo "Missing 12-day frozen zones: $CAL" >&2
  exit 2
fi
if [ ! -d "$DATA/book/XAU" ] || [ ! -d "$DATA/book/PAXG" ] || [ ! -d "$DATA/trades/PAXG" ]; then
  echo "Missing local XAU/PAXG order books or finalized PAXG public trades." >&2
  exit 2
fi

echo "Checking maker queue and causal execution unit tests..."
python3 -m pytest -q tests/test_lighter_causal_maker_first_15d.py

echo "Running frozen TP 2 and 3 causal maker-first historical replay..."
python3 -m scripts.replay_lighter_causal_maker_first_15d \
  --data "$DATA" \
  --zones "$CAL" \
  --output "$OUT"

mkdir -p "/mnt/c/Users/June-ASUS/Downloads"
python3 -m zipfile -c "$DEST" \
  "$OUT/causal_maker_first_summary.json" \
  "$OUT/causal_tp2_tp3_scenarios.csv" \
  "$OUT/causal_closed_trades.csv" \
  "$OUT/causal_terminal_positions.csv"

echo "Upload this ZIP:"
echo 'C:\Users\June-ASUS\Downloads\LIGHTER_CAUSAL_MAKER_TP2_TP3_15D.zip'
echo "Research replay only. Tape-matched maker execution and partial XAU hedges remain hypothetical."
