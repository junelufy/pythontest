#!/usr/bin/env python3
"""Calibrate XAU/PAXG L2 spread on ONLY the first 5 of 27 days.

No trading simulation, optimization over test data, or API calls.
The later 22-day out-of-sample interval must remain untouched until
the user chooses and freezes entry/exit thresholds.
"""
import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.analyze_lighter_spread_feasibility import (
    ms, iso, parse_book, paired, executable, quantiles,
)

UTC = timezone.utc
DEFAULT_START = "2026-10-23T01:00:00Z"
DEFAULT_CALIBRATION_DAYS = 5
DEFAULT_TEST_END = "2026-11-19T00:00:00Z"  # last canonical minute: Nov 18 23:59 UTC


def summary_stats(values):
    if not values:
        return {"n": 0, "min": None, "max": None, "mean": None,
                "median": None, "percentiles": {}}
    return {
        "n": len(values),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "median": round(statistics.median(values), 6),
        "percentiles": quantiles(values),
    }


def histogram(values, width=1.0):
    if not values:
        return []
    counts = defaultdict(int)
    for value in values:
        counts[math.floor(value / width) * width] += 1
    total = len(values)
    return [{"lower_usd_per_oz": round(lower, 6),
             "upper_usd_per_oz": round(lower + width, 6),
             "minutes": count,
             "fraction": round(count / total, 8)}
            for lower, count in sorted(counts.items())]


def read_calibration_books(root, symbol, start_ms, cutoff_ms):
    """Open only chunk files intersecting the first five days, NEVER holdout files."""
    directory = root / "book" / symbol
    if not directory.is_dir():
        raise FileNotFoundError("Missing L2 directory " + str(directory))
    books = {}
    checked = 0
    skipped_future = 0
    for source in sorted(directory.glob("*.jsonl.gz")):
        name = source.name.removesuffix(".jsonl.gz")
        parts = name.split("_")
        if len(parts) != 2 or not all(part.isdecimal() for part in parts):
            raise ValueError("Unexpected L2 chunk filename (cannot enforce holdout): " + source.name)
        chunk_start, chunk_end = (int(x) for x in parts)
        if chunk_start >= cutoff_ms:
            skipped_future += 1
            continue
        if chunk_end <= start_ms - 180000:
            continue
        checked += 1
        with gzip.open(source, "rt", encoding="utf8") as inp:
            for line in inp:
                if not line.strip():
                    continue
                row = json.loads(line)
                stamp = ms(row["timestamp"])
                if start_ms - 180000 <= stamp < cutoff_ms:
                    book = parse_book(row)
                    books[book.ts] = book
    if not books:
        raise ValueError("No L2 snapshots available during first five days for " + symbol)
    result = [books[key] for key in sorted(books)]
    print(f"{symbol}: calibration chunk files read={checked}; "
          f"holdout chunk files never opened={skipped_future}; "
          f"snapshots={len(result)}", flush=True)
    return [b.ts for b in result], result


def make_calibration(root, output, start_ms, days, quantities, max_quote_age_sec,
                     sampling_minutes):
    if days <= 0 or sampling_minutes <= 0 or max_quote_age_sec <= 0:
        raise ValueError("days, max quote age, and sampling step must be positive")
    if not quantities or min(quantities) <= 0:
        raise ValueError("Quantities must all be positive")
    cutoff_ms = start_ms + days * 86400_000
    out_of_sample_end_ms = ms(DEFAULT_TEST_END)
    if out_of_sample_end_ms <= cutoff_ms:
        raise ValueError("Frozen testing period does not follow calibration")
    xau = read_calibration_books(root, "XAU", start_ms, cutoff_ms)
    paxg = read_calibration_books(root, "PAXG", start_ms, cutoff_ms)
    events = paired(xau, paxg, start_ms, cutoff_ms - 1,
                    sampling_minutes, max_quote_age_sec * 1000)
    output.mkdir(parents=True, exist_ok=True)

    expected_ticks = len(range(
        ((start_ms + 59999) // 60000) * 60000,
        cutoff_ms, sampling_minutes * 60000))
    coverage = round(len(events) / expected_ticks * 100, 3) if expected_ticks else 0
    if not events:
        raise ValueError("No paired XAU/PAXG depth snapshots inside calibration window")
    rows = []
    per_quantity = {}
    sample_hash = hashlib.sha256()
    for quantity in quantities:
        values = {"maker": [], "taker": [], "long_maker": [], "long_taker": [],
                  "last_mid": [], "close_spread": []}
        per_day = defaultdict(lambda: {"maker": [], "taker": [],
                                      "long_maker": [], "long_taker": []})
        for event in events:
            tick, xb, pb = event
            item = executable(event, quantity)
            raw_best_bid_spread = xb.bid[0][0] - pb.bid[0][0]
            maker_spread = item["maker_signal"]
            taker_spread = item["taker_signal"]
            close_spread = item["exit_spread"]
            # Long spread: BUY XAU aggressively (at Ask depth) + SELL PAXG
            # passively (offer at best Ask), or SELL PAXG aggressively (Bid depth).
            # For long entries, LOWER spread is more favorable. Neither maker
            # order is guaranteed to execute when resting at best price.
            long_maker = (item["x_buy"] - pb.ask[0][0]
                          if item["x_buy"] is not None else None)
            long_taker = (item["x_buy"] - item["p_sell"]
                          if item["x_buy"] is not None and item["p_sell"] is not None
                          else None)
            sample = {
                "utc": iso(tick),
                "ts_ms": tick,
                "qty_oz_each_leg": quantity,
                "xau_snapshot_utc": iso(xb.ts),
                "paxg_snapshot_utc": iso(pb.ts),
                "xau_quote_age_s": round((tick - xb.ts) / 1000, 3),
                "paxg_quote_age_s": round((tick - pb.ts) / 1000, 3),
                "snapshot_skew_s": round(abs(xb.ts - pb.ts) / 1000, 3),
                "xau_best_bid": xb.bid[0][0],
                "paxg_best_bid": pb.bid[0][0],
                "paxg_best_ask": pb.ask[0][0],
                "xau_sell_vwap": item["x_sell"],
                "paxg_buy_taker_vwap": item["p_buy"],
                "xau_buy_vwap_for_close": item["x_buy"],
                "paxg_sell_vwap_for_close": item["p_sell"],
                "raw_best_bid_spread_usd_oz": round(raw_best_bid_spread, 6),
                "maker_entry_signal_usd_oz": round(maker_spread, 6) if maker_spread is not None else None,
                "taker_entry_signal_usd_oz": round(taker_spread, 6) if taker_spread is not None else None,
                "long_maker_entry_signal_usd_oz": round(long_maker, 6) if long_maker is not None else None,
                "long_taker_entry_signal_usd_oz": round(long_taker, 6) if long_taker is not None else None,
                "taker_close_spread_usd_oz": round(close_spread, 6) if close_spread is not None else None,
            }
            rows.append(sample)
            sample_hash.update(json.dumps(sample, sort_keys=True, separators=(",", ":")).encode())
            values["last_mid"].append(raw_best_bid_spread)
            day = sample["utc"][:10]
            if maker_spread is not None:
                values["maker"].append(maker_spread)
                per_day[day]["maker"].append(maker_spread)
            if taker_spread is not None:
                values["taker"].append(taker_spread)
                per_day[day]["taker"].append(taker_spread)
            if long_maker is not None:
                values["long_maker"].append(long_maker)
                per_day[day]["long_maker"].append(long_maker)
            if long_taker is not None:
                values["long_taker"].append(long_taker)
                per_day[day]["long_taker"].append(long_taker)
            if close_spread is not None:
                values["close_spread"].append(close_spread)
        maker_stats = summary_stats(values["maker"])
        taker_stats = summary_stats(values["taker"])
        long_maker_stats = summary_stats(values["long_maker"])
        long_taker_stats = summary_stats(values["long_taker"])
        percentile_guide = {}
        for key in ("50", "75", "90", "95", "99"):
            candidate = maker_stats["percentiles"].get(key)
            if candidate is None:
                continue
            percentile_guide[key] = {
                "level_usd_per_oz": candidate,
                "historical_minutes_at_or_above": sum(x >= candidate for x in values["maker"]),
                "label": "calibration candidate only; not a validated entry grid",
            }
        long_entry_zone_candidates = {}
        for key in ("1", "5", "10", "25"):
            candidate = long_maker_stats["percentiles"].get(key)
            if candidate is None:
                continue
            long_entry_zone_candidates[key] = {
                "level_usd_per_oz": candidate,
                "historical_minutes_at_or_below": sum(x <= candidate for x in values["long_maker"]),
                "label": "LOW tail: calibration candidate only; not a verified long-spread grid",
            }
        per_quantity[str(quantity)] = {
            "qty_oz_each_leg": quantity,
            "paired_ticks": len(events),
            "maker_signal_ticks_with_enough_l2": len(values["maker"]),
            "taker_signal_ticks_with_enough_l2": len(values["taker"]),
            "long_maker_signal_ticks_with_enough_l2": len(values["long_maker"]),
            "long_taker_signal_ticks_with_enough_l2": len(values["long_taker"]),
            "close_spread_ticks_with_enough_l2": len(values["close_spread"]),
            "maker_signal_xau_sell_vwap_minus_paxg_best_bid": maker_stats,
            "taker_signal_xau_sell_vwap_minus_paxg_buy_vwap": taker_stats,
            "long_maker_signal_xau_buy_vwap_minus_paxg_best_ask": long_maker_stats,
            "long_taker_signal_xau_buy_vwap_minus_paxg_sell_vwap": long_taker_stats,
            "raw_best_bid_spread": summary_stats(values["last_mid"]),
            "taker_close_spread_xau_buy_vwap_minus_paxg_sell_vwap": summary_stats(values["close_spread"]),
            "maker_quantile_entry_zone_candidates": percentile_guide,
            "short_spread_upper_tail_entry_candidates": percentile_guide,
            "long_spread_lower_tail_entry_candidates": long_entry_zone_candidates,
            "daily": {
                date: {
                    "maker_signal": summary_stats(day_values["maker"]),
                    "taker_signal": summary_stats(day_values["taker"]),
                    "long_maker_signal": summary_stats(day_values["long_maker"]),
                    "long_taker_signal": summary_stats(day_values["long_taker"]),
                }
                for date, day_values in sorted(per_day.items())
            },
            "histogram": {
                "maker_signal": histogram(values["maker"]),
                "taker_signal": histogram(values["taker"]),
                "long_maker_signal": histogram(values["long_maker"]),
                "long_taker_signal": histogram(values["long_taker"]),
            },
        }

    sample_file = output / "calibration_5d_spread_samples.csv"
    with sample_file.open("w", newline="", encoding="utf8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = {
        "status": "CALIBRATION_ONLY_NO_TRADING",
        "calibration_start_inclusive_utc": iso(start_ms),
        "calibration_end_exclusive_utc": iso(cutoff_ms),
        "reserved_out_of_sample_start_inclusive_utc": iso(cutoff_ms),
        "reserved_out_of_sample_end_exclusive_utc": iso(out_of_sample_end_ms),
        "calibration_days": days,
        "reserved_test_calendar_dates": 22,
        "reserved_test_duration_hours": round((out_of_sample_end_ms - cutoff_ms) / 3600000, 3),
        "intended_full_period_hours": round((out_of_sample_end_ms - start_ms) / 3600000, 3),
        "expected_sample_ticks": expected_ticks,
        "paired_fresh_snapshot_ticks": len(events),
        "paired_tick_coverage_percent": coverage,
        "first_observed_pair_utc": iso(events[0][0]),
        "last_observed_pair_utc": iso(events[-1][0]),
        "sampling_minutes": sampling_minutes,
        "max_quote_age_seconds": max_quote_age_sec,
        "sample_sha256": sample_hash.hexdigest(),
        "by_order_quantity": per_quantity,
        "methodology": [
            "This command reads raw L2 only up to the calibration cutoff; no 22-day test observations are accessed.",
            "SHORT SPREAD entry when observed spread is abnormally HIGH: SELL XAU + BUY PAXG.",
            "Short maker signal = XAU executable sell VWAP minus PAXG best BID (hypothetical resting maker BUY).",
            "Short taker signal = XAU sell VWAP minus PAXG executable buy VWAP.",
            "LONG SPREAD entry when observed spread is abnormally LOW: BUY XAU + SELL PAXG.",
            "Long maker signal = XAU executable buy VWAP minus PAXG best ASK (hypothetical resting maker SELL).",
            "Long taker signal = XAU buy VWAP minus PAXG executable sell VWAP.",
            "Use upper-tail percentiles for SHORT and lower-tail percentiles for LONG, not the zero line.",
            "Signed spread in USD per ounce; do not replace negatives with zero.",
            "Each tick uses the latest snapshot not later than the tick, with the recorded max quote age.",
            "The signal is NOT net entry/exit profit; fee/funding and adverse selection are excluded.",
            "Makers do not fill instantly: these are calibration zones, not a simulated maker strategy result.",
            "Min/max can be outliers; percentiles and daily recurrence matter more for grid selection.",
            "Calibrating thresholds and subsequently changing them on test data invalidates out-of-sample inference.",
        ],
    }
    out = output / "calibration_5d_summary.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("FIRST 5 DAYS:", iso(start_ms), "through", iso(cutoff_ms), "exclusive")
    print("RESERVED ~22 DAYS (22 calendar dates):", iso(cutoff_ms), "through", iso(out_of_sample_end_ms), "exclusive")
    print("PAIR COVERAGE:", len(events), "/", expected_ticks, f"({coverage}%)")
    for quantity in quantities:
        r = per_quantity[str(quantity)]
        maker = r["maker_signal_xau_sell_vwap_minus_paxg_best_bid"]
        taker = r["taker_signal_xau_sell_vwap_minus_paxg_buy_vwap"]
        longmaker = r["long_maker_signal_xau_buy_vwap_minus_paxg_best_ask"]
        longtaker = r["long_taker_signal_xau_buy_vwap_minus_paxg_sell_vwap"]
        print(f"QTY {quantity} oz: Short Maker-signal range {maker['min']}..{maker['max']}, "
              f"P50={maker['percentiles'].get('50')}, P75={maker['percentiles'].get('75')}, "
              f"P90={maker['percentiles'].get('90')}, P95={maker['percentiles'].get('95')}")
        print(f"QTY {quantity} oz: Short Taker-signal range {taker['min']}..{taker['max']}, "
              f"P50={taker['percentiles'].get('50')}, P90={taker['percentiles'].get('90')}")
        print(f"QTY {quantity} oz: Long Maker-signal range {longmaker['min']}..{longmaker['max']}, "
              f"P5={longmaker['percentiles'].get('5')}, P10={longmaker['percentiles'].get('10')}, "
              f"P25={longmaker['percentiles'].get('25')}")
        print(f"QTY {quantity} oz: Long Taker-signal range {longtaker['min']}..{longtaker['max']}, "
              f"P5={longtaker['percentiles'].get('5')}, P10={longtaker['percentiles'].get('10')}")
    print("SUMMARY:", out)
    print("SAMPLES:", sample_file)
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--output", default="")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--calibration-days", type=int, default=DEFAULT_CALIBRATION_DAYS)
    p.add_argument("--max-quote-age", type=int, default=120)
    p.add_argument("--step-minutes", type=int, default=1)
    p.add_argument("--quantities", type=float, nargs="+", default=[0.1, 0.5, 1.0])
    a = p.parse_args(argv)
    if a.calibration_days != DEFAULT_CALIBRATION_DAYS:
        p.error("For this locked first-pass study, use exactly 5 calibration days")
    if ms(a.start) != ms(DEFAULT_START):
        p.error("First-pass train/test split is locked to 2026-10-23T01:00:00Z")
    root = Path(a.input).expanduser().resolve()
    dest = Path(a.output).expanduser() if a.output else root / "CALIBRATION_5D"
    return make_calibration(root, dest, ms(a.start), a.calibration_days,
                            a.quantities, a.max_quote_age, a.step_minutes)


if __name__ == "__main__":
    main()
