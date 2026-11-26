#!/usr/bin/env bash
# WSL window #2; OFFLINE PAPER BACKTEST ONLY. No Lighter keys/orders/API.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

SOURCE_1S="data/lighter_1s_20261122"
SOURCE_OLD="data/lighter_fresh_20261122"
FUNDING_CACHE="${FUNDING_CACHE:-${SOURCE_OLD}/MODE_GRID_TP4_TO_TP7_FUNDING_1S/funding_hourly_1h.csv}"
WIN_DOWNLOADS="/mnt/c/Users/June-ASUS/Downloads"
mkdir -p "$WIN_DOWNLOADS"
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT="${SOURCE_OLD}/MODE_GRID_VNEXT_${RUN_TAG}"
ZIP="${WIN_DOWNLOADS}/LIGHTER_GRID_VNEXT_BUFFER1_DWELL10_HEDGE2S_${RUN_TAG}.zip"

for must in "${SOURCE_1S}/download_summary.json" "${SOURCE_1S}/chunks_manifest.csv"; do
  if [[ ! -s "$must" ]]; then echo "Missing historical source: $must" >&2; exit 2; fi
done
if [[ ! -d "${SOURCE_OLD}/trades/PAXG" ]]; then
  echo "Missing finalized PAXG public trade tape; abort" >&2; exit 2
fi
if [[ ! -s "$FUNDING_CACHE" ]]; then
  echo "WARNING: verified funding cache unavailable: $FUNDING_CACHE"
  echo "Funding-adjusted results will be NULL/UNKNOWN, NOT zero"
fi

printf 'OFFLINE_VNEXT_CONFIG: dwell=%s seconds, buffer=%s USD/oz, post_ack=%s ms, cancel_ack=%s ms, hedge_latency=%s ms, pending_cap=%s, create_budget=%s/min, cancel_budget=%s/min\n' \
  "${DWELL_SECONDS:-10}" "${CANCEL_BUFFER_USD:-1}" "${POST_LATENCY_MS:-1000}" \
  "${CANCEL_LATENCY_MS:-1000}" "${HEDGE_LATENCY_MS:-2000}" \
  "${PENDING_CAP:-2}" "${POST_PER_MINUTE:-20}" "${CANCEL_PER_MINUTE:-30}"

python3 -m pytest -q \
  tests/test_backtest_lighter_grid_vnext_1s.py \
  tests/test_backtest_lighter_mode_grid_1s.py \
  tests/test_replay_lighter_1s_frozen_tp2_tp3.py \
  tests/test_lighter_causal_maker_first_15d.py

EXTRA=()
if [[ "${STRICT_EXACT_ONLY:-0}" == "1" ]]; then EXTRA+=(--strict-exact-only); fi
if [[ "${REARM_AFTER_LEAVING_GRID:-0}" == "1" ]]; then EXTRA+=(--rearm-after-leaving-grid); fi
read -r -a TP_ARR <<< "${TP_VALUES:-2 3 4 5 6 7}"
read -r -a QTY_ARR <<< "${QUANTITIES:-0.5 1.0}"

python3 -m scripts.backtest_lighter_grid_vnext_1s \
  --source-1s "$SOURCE_1S" --old-source "$SOURCE_OLD" --output "$OUTPUT" \
  --funding-csv "$FUNDING_CACHE" \
  --hist-bin-usd "${HIST_BIN_USD:-1.0}" --grid-step-usd "${GRID_STEP_USD:-1.0}" \
  --dwell-seconds "${DWELL_SECONDS:-10}" \
  --cancel-buffer-usd "${CANCEL_BUFFER_USD:-1}" \
  --post-latency-ms "${POST_LATENCY_MS:-1000}" \
  --cancel-latency-ms "${CANCEL_LATENCY_MS:-1000}" \
  --hedge-latency-ms "${HEDGE_LATENCY_MS:-2000}" \
  --pending-cap "${PENDING_CAP:-2}" \
  --post-per-minute "${POST_PER_MINUTE:-20}" \
  --cancel-per-minute "${CANCEL_PER_MINUTE:-30}" \
  --max-age-ms "${MAX_AGE_MS:-3000}" \
  --max-exposure-oz "${MAX_EXPOSURE_OZ:-6.0}" \
  --qty "${QTY_ARR[@]}" --tp "${TP_ARR[@]}" "${EXTRA[@]}"

python3 -m zipfile -c "$ZIP" \
  "$OUTPUT/summary.json" \
  "$OUTPUT/scenarios.csv" \
  "$OUTPUT/closed_trades.csv" \
  "$OUTPUT/terminal_positions.csv" \
  "$OUTPUT/order_event_audit.csv" \
  "$OUTPUT/grid_levels.csv" \
  "$OUTPUT/histogram_7d.csv"

echo "RESULT_ZIP: $ZIP"
echo "UPLOAD: C:\\Users\\June-ASUS\\Downloads\\$(basename "$ZIP")"
echo "OUTPUT_WSL: $OUTPUT"
echo 'No real orders sent, no API calls, historical paper fill assumptions only.'
