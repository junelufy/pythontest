#!/usr/bin/env bash
# WSL #2. Historical data ONLY; read-only against 0xArchive. No trades.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${OXARCHIVE_API_KEY:-}" ]]; then
  if [[ ! -t 0 ]]; then
    echo "OXARCHIVE_API_KEY missing and no interactive terminal" >&2
    exit 2
  fi
  read -rsp "0xArchive API key (hidden): " OXARCHIVE_API_KEY
  echo
  if [[ -z "$OXARCHIVE_API_KEY" ]]; then
    echo "Empty 0xArchive API key; canceled without API calls." >&2
    exit 2
  fi
  export OXARCHIVE_API_KEY
fi

echo "Offline 1s data-downloader regression tests..."
python3 -m pytest -q tests/test_download_lighter_l2_1s_full.py

echo "First, verify 1-second source data using just 2 API calls..."
python3 scripts/download_lighter_l2_1s_full.py --probe-only

echo "Download or resume XAU and PAXG 1-second L2 across frozen 15-day window..."
python3 scripts/download_lighter_l2_1s_full.py

echo
echo "FULL SOURCE L2 (saved in WSL, gzipped files):"
echo "$(pwd)/data/lighter_1s_20261122/l2_1s/"
echo
echo "UPLOAD SUMMARY REPORT ONLY (does not contain the 2.6M price snapshots):"
echo 'C:\Users\June-ASUS\Downloads\LIGHTER_1S_15D_DOWNLOAD_REPORT.zip'
echo "Do not publish your API key or private credential files."
