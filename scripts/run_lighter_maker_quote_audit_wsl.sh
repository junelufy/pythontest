#!/usr/bin/env bash
# Forensic evidence export. Does not change strategy, download data, or run trades.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="data/lighter_fresh_20261122"
PRIOR="$DATA/FORWARD_TP2_TP3_15D"
OUT="$DATA/FORENSIC_MAKER_QUOTES_15D"
DEST="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_15D_MAKER_QUOTE_FORENSIC.zip"

echo "Running audit unit and integration tests..."
python3 -m pytest -q tests/test_lighter_maker_quote_audit.py

echo "Auditing historical as-of quote ages, XAU/PAXG clock skew, and PAXG trade prints..."
python3 -m scripts.audit_lighter_forward_maker_15d \
  --data "$DATA" \
  --prior "$PRIOR" \
  --output "$OUT"

mkdir -p "/mnt/c/Users/June-ASUS/Downloads"
python3 -m zipfile -c "$DEST" \
  "$OUT/maker_quote_forensic_summary.json" \
  "$OUT/all_maker_entry_evidence.csv" \
  "$OUT/incident_20261114_2055_quotes.csv" \
  "$OUT/incident_20261114_2055_public_paxg_trades.csv"

echo "AUDIT READY:"
echo 'C:\Users\June-ASUS\Downloads\LIGHTER_15D_MAKER_QUOTE_FORENSIC.zip'
echo "Maker fills are NOT verified from public trades; this is an independent feasibility audit."
