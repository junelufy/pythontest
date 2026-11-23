#!/usr/bin/env bash
# WSL #2: frozen-grid 1-second historical causal maker-first TP2/TP3 replay.
# Reads LOCAL 1s L2 and existing PAXG taker trades. No API calls or real trades.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA_1S="data/lighter_1s_20261122"
DATA_1M="data/lighter_fresh_20261122"
OUT="$DATA_1M/CAUSAL_MAKER_FIRST_1S_TP2_TP3"
DEST="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_1S_CAUSAL_TP2_TP3_RESULTS.zip"
CAL="$DATA_1M/CALIBRATION_12D_TWO_SIDED/zone_12d_summary.json"

for needed in "$DATA_1S/chunks_manifest.csv" "$DATA_1S/download_summary.json" "$CAL"; do
  if [[ ! -s "$needed" ]]; then echo "Missing: $needed" >&2; exit 2; fi
done
if [[ ! -d "$DATA_1M/trades/PAXG" ]]; then
  echo "Missing finalized PAXG public taker tape at $DATA_1M/trades/PAXG" >&2
  exit 2
fi

echo "Run offline engine + streaming 1s regression tests..."
python3 -m pytest -q tests/test_lighter_causal_maker_first_15d.py tests/test_replay_lighter_1s_frozen_tp2_tp3.py
echo "Running four historical paths: 0.5/1.0oz x TP2/TP3, three fee profiles..."
python3 -m scripts.replay_lighter_1s_frozen_tp2_tp3 --source-1s "$DATA_1S" --old-source "$DATA_1M" --zones "$CAL" --output "$OUT"

mkdir -p "$(dirname "$DEST")"
python3 -m zipfile -c "$DEST" \
  "$OUT/summary_1s.json" \
  "$OUT/scenarios_1s.csv" \
  "$OUT/closed_trades_1s.csv" \
  "$OUT/terminal_positions_1s.csv" \
  "$OUT/maker_event_audit_1s.csv"
echo "Upload this results ZIP to ChatGPT:"
echo "C:\Users\June-ASUS\Downloads\LIGHTER_1S_CAUSAL_TP2_TP3_RESULTS.zip"
echo "HISTORICAL REPLAY ONLY. Not actual live forward test or proven maker executions."
