#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${OXARCHIVE_API_KEY:-}" ]; then
  read -rsp "0xArchive API Key (not displayed): " OXARCHIVE_API_KEY
  echo
  export OXARCHIVE_API_KEY
fi

# Verify every API data type with a tiny request before spending credits on 30 days.
echo "First validating XAU/PAXG book, candles, funding and trades on a short interval."
python3 scripts/download_lighter_oxarchive.py --days 0.04 --symbols XAU PAXG --granularity checkpoint --depth 20 --output data/lighter_probe_all

OUT="data/lighter_fresh_20261122"
mkdir -p "$OUT"
echo "Collecting NEW ~29.9 days: Lighter XAU/PAXG L2 depth=20, 1m OHLCV, funding, trades."
echo "May consume API credits. Ctrl-C stops; rerun with the same output path to resume."
python3 scripts/download_lighter_oxarchive.py --days 29.9 --symbols XAU PAXG --granularity checkpoint --depth 20 --output "$OUT" 2>&1 | tee -a "$OUT/download.log"
python3 scripts/create_lighter_review.py --input "$OUT" --output /mnt/c/Users/June-ASUS/Downloads/lighter_fresh_review_20261122.zip
echo "All market data remains under: $PWD/$OUT"
echo "Upload this file for review: C:\\Users\\June-ASUS\\Downloads\\lighter_fresh_review_20261122.zip"
