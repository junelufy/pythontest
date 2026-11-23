#!/usr/bin/env bash
# WSL #2: historic 7d modal calibration + subsequent 1s XAU/PAXG paper backtest.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

SOURCE_1S="data/lighter_1s_20261122"
SOURCE_TAPE="data/lighter_fresh_20261122"
OUTPUT="${SOURCE_TAPE}/MODE_GRID_7D_1S_HEDGE2S"
DEST="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_MODE_GRID_7D_1S_HEDGE2S_RESULTS.zip"

# Parameters not yet established as immutable user rules; defaults are transparent.
HIST_BIN_USD="${HIST_BIN_USD:-1.0}"
GRID_STEP_USD="${GRID_STEP_USD:-1.0}"
MAX_QUOTE_AGE_MS="${MAX_QUOTE_AGE_MS:-3000}"
MAX_LEVELS="${MAX_LEVELS:-64}"

for must in "$SOURCE_1S/download_summary.json" "$SOURCE_1S/chunks_manifest.csv"; do
  [[ -s "$must" ]] || { echo "Missing $must" >&2; exit 2; }
done
[[ -d "$SOURCE_TAPE/trades/PAXG" ]] || { echo "Missing PAXG trade tape" >&2; exit 2; }
mkdir -p "$(dirname "$DEST")"

echo "TESTING: original maker + 1s tests and NEW mode-grid tests..."
python3 -m pytest -q \
  tests/test_lighter_causal_maker_first_15d.py \
  tests/test_replay_lighter_1s_frozen_tp2_tp3.py \
  tests/test_backtest_lighter_mode_grid_1s.py

echo "CALIBRATE_FIRST_7D then replay remaining history. No API calls or live orders."
echo "CONFIG mode_hist_bin=$HIST_BIN_USD grid_step=$GRID_STEP_USD quote_age_ms=$MAX_QUOTE_AGE_MS max_levels=$MAX_LEVELS hedge_latency_ms=2000"
python3 -m scripts.backtest_lighter_mode_grid_1s \
  --source-1s "$SOURCE_1S" --old-source "$SOURCE_TAPE" --output "$OUTPUT" \
  --hist-bin-usd "$HIST_BIN_USD" --grid-step-usd "$GRID_STEP_USD" \
  --max-quote-age-ms "$MAX_QUOTE_AGE_MS" --max-levels "$MAX_LEVELS"

python3 -m zipfile -c "$DEST" \
  "$OUTPUT/summary.json" \
  "$OUTPUT/histogram_7d.csv" \
  "$OUTPUT/grid_levels.csv" \
  "$OUTPUT/scenarios.csv" \
  "$OUTPUT/closed_trades.csv" \
  "$OUTPUT/terminal_positions.csv" \
  "$OUTPUT/maker_audit_events.csv"
echo "RESULT_ZIP $DEST"
echo 'UPLOAD C:\Users\June-ASUS\Downloads\LIGHTER_MODE_GRID_7D_1S_HEDGE2S_RESULTS.zip'
