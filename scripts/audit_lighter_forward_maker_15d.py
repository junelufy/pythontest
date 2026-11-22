#!/usr/bin/env python3
"""Read-only forensic audit of historical paired L2 quotes and PAXG maker evidence.

Uses 15-day FORWARD TEST trades CSV as a list of hypothetical orders only.
Reads 0xArchive PAXG finalized trades and raw 20-level L2 from local WSL.
Never infers that a maker order filled from a displayed L2 quote alone.
"""
import argparse
import bisect
import csv
import gzip
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median

from scripts.analyze_lighter_spread_feasibility import (
    iso, ms, read_books, asof,
)
from scripts.forwardtest_lighter_spread_grid_15d import TEST_START, TEST_END

INCIDENT_START = ms("2026-11-14T20:52:00Z")
INCIDENT_END = ms("2026-11-14T20:59:00Z")


def load_candidates(root):
    """Audit BOTH the default and strict clock-skew simulations, dedup fee-only clones."""
    records = []
    for label,sub in (("skew120","paired_age120_skew120"),("skew10","paired_age120_skew10")):
        path=root/sub/"trades_all_scenarios.csv"
        if not path.is_file():
            raise FileNotFoundError(f"Missing prior simulation file: {path}")
        with path.open(newline="",encoding="utf8") as f:
            records.extend((label,row) for row in csv.DictReader(f)
                           if row["fee_profile"]=="standard" and row["mode"]=="maker_assumed")
        path2=root/sub/"terminal_open_positions.csv"
        if path2.exists():
            with path2.open(newline="",encoding="utf8") as f:
                # Terminal CSV lacks original PAXG entry price; fetch from maker quote.
                records.extend((label,{**row,"entry_p":"","exit_utc":row.get("marked_at_utc",""),
                                      "mode":"maker_assumed"})
                               for row in csv.DictReader(f)
                               if row.get("fee_profile")=="standard"
                               and row.get("mode")=="maker_assumed")
    seen=set()
    out=[]
    for label,row in records:
        if not row.get("entry_utc"):continue
        key=(label,row["entry_utc"],row["direction"],row["grid"],
             float(row["quantity_oz"] if row.get("quantity_oz") else row["qty_oz_per_grid"]))
        if key in seen:continue
        seen.add(key)
        out.append({
            "scenario":label, "entry_ms":ms(row["entry_utc"]),
            "exit_ms":ms(row["exit_utc"]) if row.get("exit_utc") else None,
            "direction":row["direction"],"grid":row["grid"],
            "quantity_oz":key[4],
            "paxg_limit_from_replay":float(row["entry_p"]) if row.get("entry_p") else None,
            "example_gross_usd_oz":float(row["gross_usd_oz"]) if row.get("gross_usd_oz") else None,
            "example_tp_gross":float(row["tp_gross_usd_oz"]) if row.get("tp_gross_usd_oz") else None,
        })
    return sorted(out,key=lambda r:(r["entry_ms"],r["scenario"],r["direction"],r["grid"]))


def parse_trade(raw):
    """Return standardized public trade information or None on unknown archive schema."""
    def first(names):
        for key in names:
            if key in raw and raw[key] not in ("",None):
                return raw[key]
        return None
    if not isinstance(raw,dict):return None
    time=first(("timestamp","ts","time","timestamp_ms","ts_ms","trade_time","created_at"))
    price=first(("price","px","trade_price","execution_price"))
    size=first(("size","sz","qty","quantity","amount","base_amount"))
    if time is None or price is None:return None
    try:
        t=ms(time)
        p=float(price)
        s=float(size) if size is not None else None
        if not math.isfinite(p) or p<=0 or (s is not None and (not math.isfinite(s) or s<=0)):
            return None
    except (ValueError,TypeError,OverflowError):
        return None
    return {
        "ts_ms":t,"price":p,"size":s,
        "side":first(("side","direction","taker_side","aggressor_side")),
        "id":first(("trade_id","id","tid","tradeId")),
    }


def load_paxg_trade_prints(directory):
    if not directory.is_dir():
        raise FileNotFoundError("Historical PAXG finalized trades missing: "+str(directory))
    stats=Counter()
    rows=[]
    examples=[]
    unknown_examples=[]
    files=[]
    seenids=set()
    for p in sorted(directory.glob("*.jsonl.gz")):
        parts=p.name.removesuffix(".jsonl.gz").split("_")
        if len(parts)!=2 or not all(s.isdecimal() for s in parts):
            raise ValueError("Unrecognized trades chunk filename: "+p.name)
        start,end=map(int,parts)
        if end<=TEST_START or start>=TEST_END:continue
        files.append(p.name)
        with gzip.open(p,"rt",encoding="utf8") as f:
            for line in f:
                if not line.strip():continue
                stats["raw_rows"]+=1
                raw=json.loads(line)
                if len(examples)<3:examples.append(raw)
                obj=parse_trade(raw)
                if obj is None:
                    stats["unparsed"]+=1
                    if len(unknown_examples)<5:unknown_examples.append(raw)
                    continue
                if not(TEST_START<=obj["ts_ms"]<TEST_END):continue
                if obj["id"] is not None:
                    identifier=str(obj["id"])
                    if identifier in seenids:
                        stats["id_deduplicated"]+=1
                        continue
                    seenids.add(identifier)
                stats["parsed_in_window"]+=1
                rows.append(obj)
    rows.sort(key=lambda r:r["ts_ms"])
    return [x["ts_ms"] for x in rows],rows,dict(stats),files,examples,unknown_examples


def evidence_for(candidate, book, trade_times, trades, lookahead_minutes=5):
    entry=candidate["entry_ms"]
    direction=candidate["direction"]
    paxg_sell=(direction=="long")
    maker_px=(book.ask[0][0] if paxg_sell else book.bid[0][0])
    queue_at_quote=(book.ask[0][1] if paxg_sell else book.bid[0][1])
    original=candidate["paxg_limit_from_replay"]
    # Replayed price must match the book's PAXG maker price within 1 tick.
    match_to_replay = (None if original is None else abs(maker_px-original)<=0.011)
    out={"maker_action":"SELL" if paxg_sell else "BUY",
         "maker_limit_price":maker_px,
         "existing_visible_size_at_price_oz":queue_at_quote,
         "maker_price_matches_replay":match_to_replay}
    if not trade_times:
        out.update({"eligible_prints_first_60s":None,"eligible_prints_first_5m":None,
                    "eligible_size_first_60s_oz":None,"eligible_size_first_5m_oz":None,
                    "public_print_instant_fill_supported":"UNKNOWN_NO_PARSED_TRADES"})
        return out
    cutoff5=min(TEST_END,entry+lookahead_minutes*60000)
    lo=bisect.bisect_left(trade_times,entry)
    hi=bisect.bisect_left(trade_times,cutoff5)
    window=trades[lo:hi]
    def eligible(r):
        return r["price"] >= maker_px-0.011 if paxg_sell else r["price"] <= maker_px+0.011
    first60=[r for r in window if r["ts_ms"]<entry+60000 and eligible(r)]
    first5=[r for r in window if eligible(r)]
    def total(items):
        return round(sum(x["size"] for x in items if x["size"] is not None),8)
    p=out
    p.update({
        "all_paxg_trade_prints_first_60s":sum(x["ts_ms"]<entry+60000 for x in window),
        "eligible_prints_first_60s":len(first60),
        "eligible_prints_first_5m":len(first5),
        "eligible_size_first_60s_oz":total(first60) if all(r["size"] is not None for r in first60) else None,
        "eligible_size_first_5m_oz":total(first5) if all(r["size"] is not None for r in first5) else None,
        "first_eligible_public_print_utc":iso(first5[0]["ts_ms"]) if first5 else "",
        "public_print_instant_fill_supported":(
            "PUBLIC_TRADE_AT_ELIGIBLE_PRICE_WITHIN_60S__QUEUE_UNPROVEN"
            if first60 else "NO_PUBLIC_TRADE_AT_ELIGIBLE_PRICE_WITHIN_60S"
        ),
    })
    return p


def quote_info(event, max_age_ms=120000):
    t,x,p=event
    return {
        "xau_book_utc":iso(x.ts),"paxg_book_utc":iso(p.ts),
        "xau_quote_age_s":round((t-x.ts)/1000,3),
        "paxg_quote_age_s":round((t-p.ts)/1000,3),
        "snapshot_skew_s":round(abs(x.ts-p.ts)/1000,3),
        "source_skew_gt_10s":abs(x.ts-p.ts)>10000,
        "xau_bid":x.bid[0][0],"xau_ask":x.ask[0][0],
        "paxg_bid":p.bid[0][0],"paxg_ask":p.ask[0][0],
        "mid_spread":round((x.bid[0][0]+x.ask[0][0]-p.bid[0][0]-p.ask[0][0])/2,5),
    }


def run(data, prior, output):
    from scripts.analyze_lighter_spread_feasibility import executable
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    candidates=load_candidates(Path(prior))
    print("Hypothetical maker entries selected (dedup fee+TP clones):",len(candidates),flush=True)
    xt, xb = read_books(Path(data),"XAU",TEST_START,TEST_END-1)
    pt, pb = read_books(Path(data),"PAXG",TEST_START,TEST_END-1)
    tt,prints,trade_stats,files,trade_examples,unparsed=load_paxg_trade_prints(Path(data)/"trades"/"PAXG")
    stats={"candidates":len(candidates),"paired_found":0,"entry_skew_gt10":0,
           "exit_skew_gt10":0,"eligible_prints_60s":0,"no_prints_60s":0,
           "strict_simulation_candidates":0}
    details=[]
    incident_records=[]
    for sample in candidates:
        t=sample["entry_ms"]
        x=asof(xt,xb,t,120000)
        p=asof(pt,pb,t,120000)
        if x is None or p is None:
            details.append({**sample,"status":"MISSING_OR_STALE_ENTRY_L2"})
            continue
        event=t,x,p
        evidence=evidence_for(sample,p,tt,prints)
        q=quote_info(event)
        stats["paired_found"]+=1
        if q["source_skew_gt_10s"]:stats["entry_skew_gt10"]+=1
        if sample["scenario"]=="skew10":stats["strict_simulation_candidates"]+=1
        if evidence["public_print_instant_fill_supported"].startswith("PUBLIC_TRADE"):
            stats["eligible_prints_60s"]+=1
        if evidence["public_print_instant_fill_supported"].startswith("NO_PUBLIC"):
            stats["no_prints_60s"]+=1
        row={
            "scenario":sample["scenario"],
            "entry_utc":iso(t),"direction":sample["direction"],
            "grid":sample["grid"],"quantity_oz":sample["quantity_oz"],
            "example_tp_gross":sample["example_tp_gross"],
            "example_gross_usd_oz":sample["example_gross_usd_oz"],
            **q,**evidence,
        }
        if sample["exit_ms"] is not None:
            et=sample["exit_ms"]
            ex=asof(xt,xb,et,120000)
            ep=asof(pt,pb,et,120000)
            if ex and ep:
                end=quote_info((et,ex,ep))
                row.update({
                    "exit_utc":iso(et),
                    "exit_snapshot_skew_s":end["snapshot_skew_s"],
                    "exit_xau_book_utc":end["xau_book_utc"],
                    "exit_paxg_book_utc":end["paxg_book_utc"],
                    "exit_xau_quote_age_s":end["xau_quote_age_s"],
                    "exit_paxg_quote_age_s":end["paxg_quote_age_s"],
                    "exit_mid_spread":end["mid_spread"],
                    "delta_mid_spread":round(end["mid_spread"]-q["mid_spread"],5),
                    "exit_skew_gt10":end["source_skew_gt_10s"],
                })
                if end["source_skew_gt_10s"]:stats["exit_skew_gt10"]+=1
        details.append(row)
    # Focus on the known anomalous triple-maker fill at 2026-11-14 20:55 UTC.
    for tick in range(INCIDENT_START,INCIDENT_END+1,60000):
        xx=asof(xt,xb,tick,120000)
        pp=asof(pt,pb,tick,120000)
        if xx and pp:
            incident_records.append({"tick_utc":iso(tick),**quote_info((tick,xx,pp))})
        else:
            incident_records.append({"tick_utc":iso(tick),"status":"UNPAIRED_OR_STALE"})
    def write_csv(path,items):
        fields=list(dict.fromkeys(k for item in items for k in item.keys())) if items else ["status"]
        with (output/path).open("w",newline="",encoding="utf8") as out:
            w=csv.DictWriter(out,fieldnames=fields)
            w.writeheader()
            w.writerows(items)
    write_csv("all_maker_entry_evidence.csv",details)
    write_csv("incident_20261114_2055_quotes.csv",incident_records)
    around=[
        tr for tr in prints if INCIDENT_START<=tr["ts_ms"]<INCIDENT_END
    ]
    write_csv("incident_20261114_2055_public_paxg_trades.csv",
              [{**r,"utc":iso(r["ts_ms"])} for r in around])
    summary={
        "audit":"READ_ONLY source timestamp / maker-fill evidence for frozen 15-day forward test",
        "test_window":[iso(TEST_START),iso(TEST_END)],
        "coverage":stats,"paxg_trade_stats":trade_stats,
        "paxg_trade_chunks_read":len(files),
        "first_paxg_trade_utc":iso(tt[0]) if tt else None,
        "last_paxg_trade_utc":iso(tt[-1]) if tt else None,
        "example_raw_trade_records":trade_examples,
        "example_unparseable_trade_records":unparsed,
        "incident_20261114_2055_entries": [
            x for x in details if x.get("entry_utc")=="2026-11-14T20:55:00+00:00"
        ],
        "notes":[
            "Evidence at a public trade price is necessary context but NOT proof that an unsubmitted maker order would fill.",
            "The existing forward simulator assumed maker fill instantly with no order queue model, before XAU taker hedging.",
            "Maker queues and cancellations cannot be reconstructed reliably from one-minute Level-2 snapshots.",
            "Public trades may include duplicated match legs; deduplication uses trade ID only when provided.",
            "An absence of eligible public trades in the first minute seriously undermines the old instant maker-fill assumption, subject to completeness of finalized trade coverage.",
            "One-minute snapshots DO NOT prove simultaneous executable quotes across XAU and PAXG.",
            "No order IDs/acknowledgements mean actual maker fills CANNOT be verified retrospectively.",
            "All three 2026-11-14 20:55 long-grid PAXG maker SELL orders would compete at the SAME displayed best ask.",
            "Do not extrapolate results to live trading before a paper/live forward trial with actual order acknowledgements.",
        ],
    }
    (output/"maker_quote_forensic_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str))
    print("AUDIT_READY",output/"maker_quote_forensic_summary.json",flush=True)
    print("ELIGIBLE_PUBLIC_PRINTS_60S",stats["eligible_prints_60s"],
          "NO_PUBLIC_PRINTS_60S",stats["no_prints_60s"],flush=True)
    print("NOV14_INCIDENT",summary["incident_20261114_2055_entries"],flush=True)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data",required=True)
    parser.add_argument("--prior",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    run(args.data,args.prior,args.output)


if __name__=="__main__":
    main()
