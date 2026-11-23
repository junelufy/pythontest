#!/usr/bin/env python3
"""Frozen-grid TP2/TP3 Lighter 1-second historical causal maker-first replay.

This is NOT a fresh forward test: the 2026-11-04..19 period is already inspected.
Never implies actual maker execution. Uses prior PAXG public taker tape, which
cannot confirm our order acknowledgments, cancellations, queue or order IDs.

1s source is streamed in UTC timestamp order from pre-downloaded gzip files.
No network requests, no API key and no blanket 20-second XAU quote cutoff.
Fee profiles are recomputed from *identical* execution prices, not re-traded.
"""
import argparse
import csv
import gzip
import hashlib
import heapq
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from scripts.analyze_lighter_spread_feasibility import parse_book, ms, iso
from scripts.forwardtest_lighter_spread_grid_15d import (
    TEST_START, TEST_END, FEE_PROFILES, load_frozen_zones
)
from scripts.replay_lighter_causal_maker_first_15d import (
    run_scenario, load_taker_tape, write_csv
)

FIRST = "2026-11-04T01:00:00Z"
END = "2026-11-19T00:00:00Z"
CHUNKS_PER_SYMBOL = 1436
EXPECTED_SYMBOLS = ("XAU", "PAXG")
MAX_INVALID_SAMPLES = 500


@dataclass(frozen=True)
class InvalidBook:
    """A raw source book that cannot be executed; invalidate its old as-of quote."""
    ts: int
    reason: str



def hash_file(file):
    h = hashlib.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def get_manifest(source):
    summary_file = source / "download_summary.json"
    manifest_file = source / "chunks_manifest.csv"
    if not summary_file.is_file() or not manifest_file.is_file():
        raise FileNotFoundError("Missing 1s download_summary.json or chunks_manifest.csv; run collector first")
    summary = json.loads(summary_file.read_text(encoding="utf8"))
    if not summary.get("complete"):
        raise RuntimeError("1s download summary says complete=false; resume the downloader first")
    conf = summary.get("configuration", {})
    if (conf.get("start") != FIRST or conf.get("end") != END
        or conf.get("granularity") != "1s" or conf.get("depth") != 20):
        raise ValueError("1s source manifest configuration does not match frozen test interval/depth")
    records = {sym: [] for sym in EXPECTED_SYMBOLS}
    with manifest_file.open(newline="", encoding="utf8") as f:
        for row in csv.DictReader(f):
            sym = row["symbol"]
            if sym not in records:
                raise ValueError("Unexpected source symbol "+sym)
            records[sym].append(row)
    nrows = {}
    missing = {}
    for sym, rows in records.items():
        rows.sort(key=lambda r:r["start"])
        if len(rows) != CHUNKS_PER_SYMBOL:
            raise ValueError(f"Missing/duplicate {sym} 1s chunks: {len(rows)} != {CHUNKS_PER_SYMBOL}")
        if rows[0]["start"] != FIRST or rows[-1]["end"] != END:
            raise ValueError("1s source coverage misses beginning or end for "+sym)
        count = 0
        missed = 0
        for index, row in enumerate(rows):
            if index and rows[index-1]["end"] != row["start"]:
                raise ValueError(f"Missing or overlapping 1s {sym} chunks around {row['start']}")
            expected = int(row["expected_seconds"])
            actual = int(row["observed_unique_seconds"])
            stated = int(row["row_count"])
            if expected != 900 or actual != stated or not 0 < actual <= expected:
                raise ValueError(f"Invalid 1s {sym} manifest row at {row['start']}")
            count += stated
            missed += expected - actual
            if len(row["sha256"]) != 64:
                raise ValueError("Missing SHA-256 in manifest")
            path = source / "l2_1s" / sym / Path(row["file"]).name
            if not path.is_file():
                raise FileNotFoundError(f"Missing downloaded 1s source: {path}")
            if hash_file(path) != row["sha256"]:
                raise RuntimeError(f"Source SHA256 mismatch: {path}")
        nrows[sym], missing[sym] = count, missed
    return records, {"snapshots_per_symbol":nrows, "missing_seconds_per_symbol":missing,
                     "manifest_sha256":hash_file(manifest_file),
                     "source_complete":True,
                     "chunk_count_per_symbol":CHUNKS_PER_SYMBOL,
                     "quality_note":"Expected 1 missing interval-start second per chunk; verified 10 spot samples separately"}


def iter_books(source, symbol, records, counters, rejects=None):
    """Stream all *raw* rows; invalid L2 gets a tombstone, not a stale last quote.

    JSON/time/order/manifest errors remain fatal. Crossed/empty/malformed book
    prices are quarantined and counted, never repaired or used for trading.
    """
    last_raw = None
    last_valid = None
    for row in records:
        lo, hi = ms(row["start"]), ms(row["end"])
        path = source / "l2_1s" / symbol / Path(row["file"]).name
        seen = 0
        with gzip.open(path, "rt", encoding="utf8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                obj = json.loads(line)
                ts = ms(obj["timestamp"])
                if not lo <= ts < hi:
                    raise ValueError(f"Out-of-window {symbol} 1s book in {path}:{lineno}")
                if last_raw is not None:
                    if ts <= last_raw:
                        raise ValueError(f"Out-of-order/duplicate {symbol} raw 1s book: {iso(ts)}")
                    gap_ms = ts - last_raw
                    if gap_ms > 2000:
                        counters[symbol + "_raw_gaps_over_2s"] += 1
                    counters[symbol + "_largest_raw_gap_ms"] = max(
                        counters[symbol + "_largest_raw_gap_ms"], gap_ms
                    )
                last_raw = ts
                seen += 1
                counters[symbol + "_rows_seen"] += 1
                try:
                    book = parse_book(obj)
                except (ValueError, TypeError, KeyError) as exc:
                    reason = str(exc)
                    counters[symbol + "_rejected"] += 1
                    counters[symbol + ("_crossed" if reason == "Crossed order book"
                                        else "_other_invalid")] += 1
                    if rejects is not None:
                        if len(rejects) < MAX_INVALID_SAMPLES:
                            def first_price(name):
                                levels = obj.get(name, [])
                                if not isinstance(levels, list) or not levels:
                                    return None
                                level = levels[0]
                                if isinstance(level, dict):
                                    return level.get("px", level.get("price"))
                                return level[0] if isinstance(level, (list, tuple)) and level else None
                            rejects.append({
                                "symbol": symbol, "timestamp_utc": iso(ts),
                                "filename": path.name, "line_number": lineno,
                                "raw_first_bid": first_price("bids"),
                                "raw_first_ask": first_price("asks"),
                                "reject_reason": reason,
                                "action": "INVALIDATE_QUOTE_UNTIL_NEXT_VALID_BOOK",
                            })
                        if counters[symbol + "_rejected"] <= 5:
                            print("INVALID_1S_BOOK", symbol, iso(ts), repr(reason),
                                  "in", path.name, "line", lineno, flush=True)
                    yield InvalidBook(ts, reason)
                    continue
                if book.ts != ts:
                    raise AssertionError("Inconsistent source timestamp conversion")
                if last_valid is not None:
                    valid_gap = ts - last_valid
                    if valid_gap > 2000:
                        counters[symbol + "_valid_gaps_over_2s"] += 1
                    counters[symbol + "_largest_valid_gap_ms"] = max(
                        counters[symbol + "_largest_valid_gap_ms"], valid_gap
                    )
                last_valid = ts
                counters[symbol + "_parsed"] += 1
                yield book
        if seen != int(row["row_count"]):
            raise ValueError(f"1s {symbol} RAW row count changed since download: {path}")


def merge_timeline(source, records, tape, counters, rejects=None):
    def book_events(symbol, kind):
        for row in iter_books(source, symbol, records[symbol], counters, rejects=rejects):
            # Tombstone at the ORIGINAL timestamp. The engine clears active
            # quote until the next valid update; no forward fill across bad data.
            yield row.ts, kind, None if isinstance(row, InvalidBook) else row

    xevents = book_events("XAU", 0)
    pevents = book_events("PAXG", 1)
    tevents = ((t.ts, 2, t) for t in tape)
    # At identical timestamp: XAU (including invalidation), PAXG, then tape.
    return heapq.merge(xevents, pevents, tevents, key=lambda item: (item[0], item[1]))


def fees_for_trade(row, maker_bps, taker_bps, unhedged=False):
    qty = float(row["qty_oz"])
    p0 = float(row["paxg_maker_px"])
    x0 = row.get("xau_hedge_px")
    px = row.get("xau_exit_px")
    pe = float(row["paxg_exit_px"])
    maker_fee = qty*p0*maker_bps/10000.
    if unhedged:
        return maker_fee, qty*pe*taker_bps/10000.
    if x0 is None or px is None:
        raise ValueError("Missing XAU entry/exit for hedged position fee pricing")
    entry_fee = maker_fee + qty*float(x0)*taker_bps/10000.
    exit_fee = qty*(float(px)+pe)*taker_bps/10000.
    return entry_fee, exit_fee


def derive_fee_profiles(base, closed, terminal, maker_bps, taker_bps):
    """Fees never change gross TP triggers, the frozen orders, or public prints."""
    new_closed = []
    new_terminal = []
    for r in closed:
        fee_in, fee_out = fees_for_trade(r, maker_bps, taker_bps)
        gross = float(r["gross_usd"])
        new_closed.append({**r,"entry_fee_usd":fee_in,"exit_fee_usd":fee_out,
                           "net_usd":gross-fee_in-fee_out})
    for r in terminal:
        unhedged = r["status"] == "hedge_wait"
        fee_in, fee_out = fees_for_trade(r, maker_bps, taker_bps, unhedged=unhedged)
        gross = float(r["gross_usd"])
        new_terminal.append({**r,"entry_fee_usd":fee_in,"exit_fee_usd":fee_out,
                             "hypothetical_liquidation_net_usd":gross-fee_in-fee_out})
    realized = sum(t["net_usd"] for t in new_closed)
    terminal_net = sum(t["hypothetical_liquidation_net_usd"] for t in new_terminal)
    incomplete = int(base["terminal_unmarked_positions"]) != 0
    new_summary = {**base,
        "maker_bps":maker_bps,
        "taker_bps":taker_bps,
        "closed_net_usd":round(realized,6),
        "paid_entry_fee_usd":round(sum(t["entry_fee_usd"] for t in new_closed+new_terminal),6),
        "paid_exit_fee_usd":round(sum(t["exit_fee_usd"] for t in new_closed),6),
        "terminal_liquidation_net_usd":round(terminal_net,6) if not incomplete else None,
        "total_net_with_terminal_liquidation_usd":round(realized+terminal_net,6) if not incomplete else None,
        "max_complete_mark_drawdown_usd": base["max_complete_mark_drawdown_usd"]
          if maker_bps==0 and taker_bps==0 else None,
        "fee_profile_method":"repriced same gross orders/fills/TP events; no rerun; fee-profile drawdown not recalculated",
    }
    return new_summary, new_closed, new_terminal


def run(source, old_source, zones_file, output):
    source, old_source, zones_file, out = map(Path,(source, old_source, zones_file, output))
    records, quality = get_manifest(source)
    if not (old_source / "trades" / "PAXG").is_dir():
        raise FileNotFoundError("Existing archived PAXG taker tape required at "+str(old_source))
    tape, trade_quality = load_taker_tape(old_source)
    if not tape:
        raise RuntimeError("PAXG public trade tape is empty")
    out.mkdir(parents=True, exist_ok=True)
    scenarios=[]
    invalid_evidence = []
    reference_parse_quality = None
    all_closed=[]
    all_terminal=[]
    all_events=[]
    for qty in (0.5, 1.0):
        zones = load_frozen_zones(zones_file, qty)
        for tp in (2.0, 3.0):
            counters=Counter()
            audit=[] if qty==0.5 else None
            print("RUN_1S",qty,tp,"fee-free execution path",flush=True)
            baseline,closed,terminal=run_scenario(
                [],[],[],zones,qty,tp,maker_bps=0.,taker_bps=0.,
                event_log=audit,max_quote_age_ms=None,
                timeline_override=merge_timeline(
                    source, records, tape, counters,
                    rejects=invalid_evidence if qty == 0.5 and tp == 2.0 else None
                )
            )
            for fee_name,(m_bps,t_bps) in FEE_PROFILES.items():
                summary,trades,positions=derive_fee_profiles(baseline,closed,terminal,m_bps,t_bps)
                summary["fee_profile"]=fee_name
                summary["source_granularity"]="1s"
                summary["quote_filter"]="none; use only past as-of L2, source gaps audited"
                scenarios.append(summary)
                all_closed.extend([{"qty_per_grid_oz":qty,"tp_gross_usd_per_oz":tp,
                                    "fee_profile":fee_name,**r} for r in trades])
                all_terminal.extend([{"qty_per_grid_oz":qty,"tp_gross_usd_per_oz":tp,
                                      "fee_profile":fee_name,**r} for r in positions])
                print("RESULT_1S",qty,tp,fee_name,
                      "maker_posts",summary["paxg_maker_orders_posted"],
                      "paper_fills",summary["maker_orders_with_any_public_trade_allocated"],
                      "tp_closed",summary["tp_closed_positions"],
                      "closed_net",summary["closed_net_usd"],
                      "terminal_net",summary["terminal_liquidation_net_usd"],
                      "total_net",summary["total_net_with_terminal_liquidation_usd"],flush=True)
            if audit is not None:
                all_events.extend([{"qty_per_grid_oz":qty,"tp_gross_usd_per_oz":tp,**e} for e in audit])
            parse_quality = {
                sym: {
                    "raw_rows": counters[sym + "_rows_seen"],
                    "valid_books": counters[sym + "_parsed"],
                    "invalid_books": counters[sym + "_rejected"],
                    "crossed_books": counters[sym + "_crossed"],
                    "other_invalid_books": counters[sym + "_other_invalid"],
                    "raw_gaps_over_2s": counters[sym + "_raw_gaps_over_2s"],
                    "valid_gaps_over_2s": counters[sym + "_valid_gaps_over_2s"],
                    "max_raw_gap_ms": counters[sym + "_largest_raw_gap_ms"],
                    "max_valid_gap_ms": counters[sym + "_largest_valid_gap_ms"],
                } for sym in EXPECTED_SYMBOLS
            }
            for sym in EXPECTED_SYMBOLS:
                q = parse_quality[sym]
                if q["raw_rows"] != quality["snapshots_per_symbol"][sym]:
                    raise RuntimeError(f"Incomplete 1s RAW source for {sym} qty={qty} tp={tp}")
                if q["raw_rows"] != q["valid_books"] + q["invalid_books"]:
                    raise AssertionError("Unaccounted invalid data for " + sym)
            if reference_parse_quality is None:
                reference_parse_quality = parse_quality
                quality["book_integrity"] = parse_quality
                quality["invalid_sample_limit"] = MAX_INVALID_SAMPLES
                quality["quote_invalidation_policy"] = (
                    "Crossed/invalid source rows are NOT executed or price-corrected. "
                    "They invalidate the affected instrument's last as-of quote "
                    "until a subsequent valid source row. Outstanding hypothetical "
                    "maker orders are not retrospectively erased."
                )
            elif parse_quality != reference_parse_quality:
                raise RuntimeError("Source parsing quality changed between replay scenarios")
            print("SOURCE_PROGRESS", qty, tp, "quality", parse_quality, flush=True)
    report={
        "experiment":"1S_HISTORICAL_FORWARD_STYLE_REPLAY_NOT_FRESH_FORWARD_DATA",
        "date_start_utc":FIRST,"date_end_exclusive_utc":END,
        "calibration":"frozen 12 days; no grid reoptimization",
        "execution":"maker-first PAXG with public taker tape, FIFO visible queue, conditional price-through paper fills; next strictly later XAU 1s L2 hedge; spread-cancel pending maker; executable gross TP 2 or 3 USD/oz; no stops or timeouts",
        "max_age_filter":"NONE: chronological as-of only; no arbitrary 20-second cutoff",
        "source":{"manifest":quality,"taker_tape":trade_quality},
        "limitations":[
            "THIS IS NOT LIVE FORWARD TEST; November 4-19 outcomes have been inspected before replay.",
            "No user maker order acknowledgments or actual fills; public tape provides hypothetical price-through only.",
            "Even 1s L2 cannot prove microsecond order/cancel/hedge ordering or queue position.",
            "Every 15-minute file boundary lacks its first 1s record; as-of previous valid observation is used.",
            "Crossed/invalid books invalidate that venue quote (without repairing it); all rejection counts and samples are exported. This may change simulated maker/hedge opportunity.",
            "PAXG public tape uses previous separately downloaded archive. Coverage/quality are as previously supplied.",
            "TP measured GROSS per oz; fees deducted after, unchanged path across fee profiles.",
            "Fee-profile drawdown omitted rather than falsely reusing fee-free result.",
            "Terminal unrealized marked using last available L2 liquidation prices; no funding, margin, slippage, liquidation, market impact or outage costs.",
            "Scenarios are independent; never add returns across hypothetical accounts.",
        ],
        "scenarios":scenarios,
    }
    write_csv(out/"scenarios_1s.csv",scenarios,["fee_profile","qty_per_grid_oz","tp_gross_usd_per_oz"])
    write_csv(out/"closed_trades_1s.csv",all_closed,["fee_profile","net_usd"])
    write_csv(out/"terminal_positions_1s.csv",all_terminal,["fee_profile","hypothetical_liquidation_net_usd"])
    write_csv(out/"maker_event_audit_1s.csv",all_events,["type","time_utc","grid","direction"])
    write_csv(out/"invalid_book_evidence_1s.csv", invalid_evidence,
              ["symbol", "timestamp_utc", "filename", "line_number",
               "raw_first_bid", "raw_first_ask", "reject_reason", "action"])
    (out/"summary_1s.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf8")
    print("REPORT_1S",out/"summary_1s.json",flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-1s",default="data/lighter_1s_20261122")
    p.add_argument("--old-source",default="data/lighter_fresh_20261122")
    p.add_argument("--zones",default="data/lighter_fresh_20261122/CALIBRATION_12D_TWO_SIDED/zone_12d_summary.json")
    p.add_argument("--output",default="data/lighter_fresh_20261122/CAUSAL_MAKER_FIRST_1S_TP2_TP3")
    a=p.parse_args()
    run(a.source_1s,a.old_source,a.zones,a.output)


if __name__=="__main__":
    main()
