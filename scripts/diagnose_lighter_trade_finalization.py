#!/usr/bin/env python3
"""Forensic audit of Lighter historical trades finalization vs candle coverage.

Read the user's completed local dataset. Optional two tiny API history requests
inspect the provider's finalization watermark; NEVER print credentials.
No dataset deletion, backfill, or trading.
"""
import argparse
import gzip
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.audit_lighter_trade_candle_coverage import trade_ts_ms
from scripts.download_lighter_oxarchive import get, timestamp

UTC=timezone.utc

def iso(ts_ms):
    return datetime.fromtimestamp(ts_ms/1000,UTC).isoformat() if ts_ms is not None else None

def inspect(base,symbol,probe=False):
    path=base/"lighter_marketdata.sqlite3"
    with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
        candles={int(ts)//60000:float(v) if v is not None else None for ts,v in
                 db.execute("select ts_ms,base_volume from candles where symbol=?",(symbol,))}
        chunks=[{"start_ms":start,"end_ms":end,"records":count}
                for start,end,count in db.execute(
                    "select begin_ms,end_ms,rows_count from chunks "
                    "where symbol=? and kind='trades' order by begin_ms",(symbol,))]
    trade_minutes=Counter()
    files=sorted((base/"trades"/symbol).glob("*.jsonl.gz"))
    min_ts=max_ts=None
    source_counts=Counter()
    trade_id_legs=Counter()
    no_trade_id=0
    nrows=0
    for file in files:
        with gzip.open(file,"rt",encoding="utf8") as src:
            for line in src:
                if not line.strip():continue
                row=json.loads(line)
                ts=trade_ts_ms(row)
                min_ts=ts if min_ts is None else min(min_ts,ts)
                max_ts=ts if max_ts is None else max(max_ts,ts)
                trade_minutes[ts//60000]+=1
                source_counts[str(row.get("source","missing"))]+=1
                tid=row.get("trade_id",row.get("tid"))
                if tid is not None:
                    trade_id_legs[str(tid)]+=1
                else:
                    no_trade_id+=1
                nrows+=1
    unpaired=sorted(m for m,v in candles.items() if v is not None and v>0 and m not in trade_minutes)
    daily=Counter(iso(m*60000)[:10] for m in unpaired)
    empty_chunks=[]
    for c in chunks:
        if c["records"]==0:
            start=c["start_ms"]//60000
            end=(c["end_ms"]-1)//60000
            positive=sum(1 for m,v in candles.items() if start<=m<=end and v is not None and v>0)
            empty_chunks.append({"start_utc":iso(c["start_ms"]),"end_utc":iso(c["end_ms"]),
                                 "positive_volume_candle_minutes":positive})
    report={
        "symbol":symbol,"raw_trade_rows":nrows,
        "first_raw_trade_utc":iso(min_ts),"last_raw_trade_utc":iso(max_ts),
        "raw_trade_source_counts":dict(source_counts),
        "rows_with_trade_id":nrows-no_trade_id,
        "rows_without_trade_id":no_trade_id,
        "distinct_trade_ids":len(trade_id_legs),
        "fill_legs_per_trade_id":dict(Counter(trade_id_legs.values())),
        "interpretation":"Historical Lighter trade rows are individual fill legs; a matched trade can have two legs with the same trade_id. Do not sum both sides for traded volume.",
        "total_trade_chunks":len(chunks),"zero_row_trade_chunks":len(empty_chunks),
        "zero_row_chunk_details":empty_chunks,
        "positive_candle_minutes_without_raw_trade":len(unpaired),
        "unpaired_by_utc_day":dict(sorted(daily.items())),
        "first_unpaired_utc":iso(unpaired[0]*60000) if unpaired else None,
        "last_unpaired_utc":iso(unpaired[-1]*60000) if unpaired else None,
        "api_probe":None
    }
    if probe:
        key=os.getenv("OXARCHIVE_API_KEY","").strip()
        if not key:raise RuntimeError("Missing OXARCHIVE_API_KEY; skip --probe if offline")
        if not candles:raise ValueError("No candles available to choose probe interval")
        # Select a recent, positive-volume 1-minute candle from the saved archive.
        end_minute=max(m for m,v in candles.items() if v is not None and v>0)
        start_ms=end_minute*60000
        params={"start":start_ms,"end":start_ms+60000,"limit":50}
        packet=get(f"/v1/lighter/trades/{symbol}",params,key)
        meta=packet.get("meta",{})
        report["api_probe"]={
            "requested_start_utc":iso(start_ms),
            "requested_end_utc":iso(start_ms+60000),
            "returned_rows":len(packet["data"]),
            "finalized_through":meta.get("finalized_through",meta.get("finalizedThrough")),
            "clamped_to":meta.get("clamped_to",meta.get("clampedTo")),
            "requested_end":meta.get("requested_end",meta.get("requestedEnd")),
            "meta_keys":sorted(meta),
            "note":"The history endpoint serves finalized only; /recent serves preliminary."
        }
        # Classify whether gaps are before/after canonical finalization when available.
        final=report["api_probe"]["finalized_through"]
        if final:
            cutoff=timestamp(final)//60000
            before=[m for m in unpaired if m<cutoff]
            after=[m for m in unpaired if m>=cutoff]
            report["unpaired_positive_candles_before_finalized_through"]=len(before)
            report["unpaired_positive_candles_at_or_after_finalized_through"]=len(after)
            report["before_watermark_examples_utc"]=[iso(m*60000) for m in before[:10]]
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--input",required=True)
    p.add_argument("--symbols",nargs="+",default=["PAXG","XAU"])
    p.add_argument("--probe",action="store_true",
                   help="Use one historical trades API request per symbol to inspect meta.finalized_through")
    p.add_argument("--output",default=None)
    a=p.parse_args()
    base=Path(a.input).expanduser().resolve()
    result={}
    for symbol in a.symbols:
        print("Inspecting",symbol,flush=True)
        result[symbol]=inspect(base,symbol,a.probe)
        print(symbol,"last trade",result[symbol]["last_raw_trade_utc"],
              "zero chunks",result[symbol]["zero_row_trade_chunks"],
              "unpaired positive candles",result[symbol]["positive_candle_minutes_without_raw_trade"],
              "meta",result[symbol]["api_probe"],flush=True)
    dest=Path(a.output).expanduser() if a.output else base/"LIGHTER_TRADE_FINALIZATION_AUDIT.json"
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print("AUDIT_SAVED",dest)

if __name__=="__main__":
    main()
