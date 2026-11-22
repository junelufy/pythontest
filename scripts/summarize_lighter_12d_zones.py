#!/usr/bin/env python3
"""Summarize mean daily 12-day LONG/SHORT entry zones without touching holdout.

Input is the FIRST-TWELVE-DAYS-ONLY CSV from calibrate_lighter_spread_5d.py.
A 'day' is 24 consecutive hours beginning Oct 23, 2026 01:00 UTC:
not an arbitrary midnight-to-midnight date with partial boundary days.
This reports candidate zones for review, not chosen/frozen trade rules.
"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from scripts.analyze_lighter_spread_feasibility import ms, iso, quantiles

DAY_MS=86400_000
FROZEN_START=ms("2026-10-23T01:00:00Z")
FROZEN_END=FROZEN_START+12*DAY_MS
SIGNALS={
    "short_maker":"maker_entry_signal_usd_oz",
    "short_taker":"taker_entry_signal_usd_oz",
    "long_maker":"long_maker_entry_signal_usd_oz",
    "long_taker":"long_taker_entry_signal_usd_oz",
}
ZONE_KEYS={
    "short_maker":("75","90","95"),
    "short_taker":("75","90","95"),
    "long_maker":("5","10","25"),
    "long_taker":("5","10","25"),
}

def compute(samples_path, summary_path, output_dir):
    summary=json.loads(Path(summary_path).read_text(encoding="utf8"))
    assert summary["calibration_days"]==12, "Expected a 12-day calibration summary"
    assert ms(summary["calibration_start_inclusive_utc"])==FROZEN_START
    assert ms(summary["calibration_end_exclusive_utc"])==FROZEN_END
    assert ms(summary["reserved_out_of_sample_start_inclusive_utc"])==FROZEN_END
    assert ms(summary["reserved_out_of_sample_end_exclusive_utc"])==ms("2026-11-19T00:00:00Z")
    samples=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    minute_ticks=defaultdict(lambda:defaultdict(set))
    observed=0
    with Path(samples_path).open(newline="",encoding="utf8") as f:
        for row in csv.DictReader(f):
            t=int(row["ts_ms"])
            if not (FROZEN_START<=t<FROZEN_END):
                raise ValueError("Calibration CSV includes data outside frozen first 12 days")
            ix=(t-FROZEN_START)//DAY_MS
            qty=str(float(row["qty_oz_each_leg"]))
            minute_ticks[qty][ix].add(t)
            for name,column in SIGNALS.items():
                raw=row.get(column)
                if raw not in ("",None):
                    samples[qty][ix][name].append(float(raw))
            observed+=1
    output_dir=Path(output_dir)
    output_dir.mkdir(parents=True,exist_ok=True)
    records=[]
    per_size={}
    for qty in sorted(samples,key=float):
        zones={}
        per24=[]
        for ix in range(12):
            date_start=FROZEN_START+ix*DAY_MS
            observed_minutes=len(minute_ticks[qty][ix])
            entry={"window_index":ix+1,"start_utc":iso(date_start),
                   "end_exclusive_utc":iso(date_start+DAY_MS),
                   "qty_oz":float(qty),"paired_minutes":observed_minutes,
                   "coverage_pct":round(observed_minutes/1440*100,3)}
            for name in SIGNALS:
                seq=samples[qty][ix][name]
                q=quantiles(seq)
                entry[name+"_ticks"]=len(seq)
                entry[name+"_min"]=min(seq) if seq else None
                entry[name+"_max"]=max(seq) if seq else None
                entry[name+"_median"]=q.get("50")
                for p in ZONE_KEYS[name]:
                    entry[name+"_P"+p]=q.get(p)
            per24.append(entry)
            records.append(entry)
        for name in SIGNALS:
            z={}
            for p in ZONE_KEYS[name]:
                levels=[window.get(name+"_P"+p) for window in per24]
                z["mean_of_12_daily_P"+p] = (round(statistics.fmean(levels),6)
                                              if all(v is not None for v in levels) else None)
                z["min_daily_P"+p] = min(levels) if all(v is not None for v in levels) else None
                z["max_daily_P"+p] = max(levels) if all(v is not None for v in levels) else None
            zones[name]=z
        per_size[qty]={
            "zones_usd_per_oz":zones,
            "paired_minute_coverage_min_daily_pct":min(day["coverage_pct"] for day in per24),
            "paired_minute_coverage_average_daily_pct":round(statistics.fmean(day["coverage_pct"] for day in per24),3),
            "same_direction_comparison_note":"Short enters high spread, long enters low spread; signals use different executable sides",
        }
    result={
        "study":"12-day equal-weight daily-zone calibration, BOTH LONG and SHORT SPREAD",
        "calibration_start_inclusive_utc":iso(FROZEN_START),
        "calibration_end_exclusive_utc":iso(FROZEN_END),
        "reserved_test_start_inclusive_utc":summary["reserved_out_of_sample_start_inclusive_utc"],
        "reserved_test_end_exclusive_utc":summary["reserved_out_of_sample_end_exclusive_utc"],
        "days":12,"observations_in_csv":observed,
        "method":"For each quantity and direction, take daily 24h P5/P10/P25 for LONG, daily P75/P90/P95 for SHORT; then average these 12 levels equally.",
        "pooled_minute_percentiles":summary["by_order_quantity"],
        "equal_day_weight_zones_by_qty":per_size,
        "candidate_only":True,
        "caveats":[
            "Zones were calibrated using ONLY 12 earlier days; the later ~15 days were NOT accessed.",
            "Median/mean alone do not define both tails; min/max are sensitive to outliers.",
            "Maker assumes resting order could fill at displayed PAXG bid/ask but this analysis does NOT verify any maker fills.",
            "The XAU leg uses 20-level executable VWAP and PAXG maker entry uses displayed best quote.",
            "Opposite directions have different executable costs; compare net bid/ask-aware trade returns only after freezing rules.",
            "A day with sparse snapshots still has equal weight; inspect coverage before accepting zones.",
        ]
    }
    with (output_dir/"zone_12d_summary.json").open("w",encoding="utf8") as f:
        json.dump(result,f,indent=2,ensure_ascii=False)
    with (output_dir/"zone_12d_daily.csv").open("w",newline="",encoding="utf8") as f:
        writer=csv.DictWriter(f,fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    for qty,z in per_size.items():
        short=z["zones_usd_per_oz"]["short_maker"]
        long=z["zones_usd_per_oz"]["long_maker"]
        print(f"QTY {qty} oz / 12-day equal-weight zones:")
        print("  SHORT spread HIGH: avg daily P75=",short["mean_of_12_daily_P75"],
              "P90=",short["mean_of_12_daily_P90"],
              "P95=",short["mean_of_12_daily_P95"])
        print("  LONG spread LOW: avg daily P5=",long["mean_of_12_daily_P5"],
              "P10=",long["mean_of_12_daily_P10"],
              "P25=",long["mean_of_12_daily_P25"])
        print("  Min daily quote coverage=",z["paired_minute_coverage_min_daily_pct"],"%")
    print("12D_ZONE_REPORT",output_dir/"zone_12d_summary.json")
    return result

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--summary",required=True)
    p.add_argument("--samples",required=True)
    p.add_argument("--output",required=True)
    a=p.parse_args()
    compute(a.samples,a.summary,a.output)
