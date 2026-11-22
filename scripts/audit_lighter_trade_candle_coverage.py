#!/usr/bin/env python3
"""Independent offline check: do missing 1m candles coincide with no raw trades?"""
import argparse
import gzip
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

KEYS=("timestamp","ts_ms","time","t","trade_time","created_at","transact_time","ts")

def trade_ts_ms(item):
    from datetime import datetime
    if not isinstance(item,dict):
        raise ValueError("Trade record must be object; got "+type(item).__name__)
    for key in KEYS:
        value=item.get(key)
        if value is None:continue
        if isinstance(value,(int,float)) or isinstance(value,str) and value.strip().replace(".","",1).isdigit():
            val=float(value)
            if val>1e17:return int(val/1e6)
            if val>1e14:return int(val/1e3)
            if val>1e11:return int(val)
            return int(val*1000)
        return int(datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()*1000)
    raise ValueError("Unknown trade timestamp keys: "+", ".join(sorted(item)[:30]))

def audit(base,symbol):
    path=base/"lighter_marketdata.sqlite3"
    if not path.is_file():raise FileNotFoundError(path)
    with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
        candle_volumes={int(r[0])//60000:(float(r[1]) if r[1] is not None else None)
                        for r in db.execute("select ts_ms,base_volume from candles where symbol=?",(symbol,))}
        candle_minutes=set(candle_volumes)
        quote_minutes={int(r[0])//60000 for r in db.execute(
            "select ts_ms from quotes where symbol=?",(symbol,))}
    files=sorted((base/"trades"/symbol).glob("*.jsonl.gz"))
    if not files:raise FileNotFoundError("No raw trade files for "+symbol)
    seen=Counter()
    raw=0
    for i,file in enumerate(files):
        with gzip.open(file,"rt",encoding="utf8") as f:
            for num,line in enumerate(f,1):
                if not line.strip():continue
                try:minute=trade_ts_ms(json.loads(line))//60000
                except (ValueError,TypeError,KeyError) as e:
                    raise ValueError(f"{file.name} line {num}: {e}") from e
                seen[minute]+=1
                raw+=1
        if i and i%25==0:print(f"{symbol}: examined {i+1}/{len(files)} raw trade files",flush=True)
    trade_minutes=set(seen)
    start=min(candle_minutes|quote_minutes)
    end=max(candle_minutes|quote_minutes)
    trade_minutes_in_window={t for t in trade_minutes if start<=t<=end}
    extra_trades=sorted(trade_minutes_in_window-candle_minutes)
    unsupported_candles=sorted(candle_minutes-trade_minutes_in_window)
    zero_vol=[m for m in unsupported_candles if candle_volumes[m]==0]
    positive_vol=[m for m in unsupported_candles if candle_volumes[m] is not None and candle_volumes[m]>0]
    unknown_vol=[m for m in unsupported_candles if candle_volumes[m] is None]
    negative_vol=[m for m in unsupported_candles if candle_volumes[m] is not None and candle_volumes[m]<0]
    trade_with_zero_candle_vol=[m for m in (trade_minutes_in_window & candle_minutes)
                                if candle_volumes[m]==0]
    candle_without_trade_by_day=Counter(datetime.fromtimestamp(m*60,timezone.utc).date().isoformat()
                                        for m in unsupported_candles)
    positive_without_trade_by_day=Counter(datetime.fromtimestamp(m*60,timezone.utc).date().isoformat()
                                        for m in positive_vol)
    report={
        "symbol":symbol,
        "trade_raw_records":raw,
        "trade_files":len(files),
        "unique_trade_minutes":len(trade_minutes),
        "candle_minutes":len(candle_minutes),
        "quote_minutes":len(quote_minutes),
        "trade_minutes_inside_candle_quote_window":len(trade_minutes_in_window),
        "trade_minutes_missing_candle":len(extra_trades),
        "candles_without_matching_trade":len(unsupported_candles),
        "candle_no_trade_zero_volume":len(zero_vol),
        "candle_no_trade_positive_volume":len(positive_vol),
        "candle_no_trade_missing_volume":len(unknown_vol),
        "candle_no_trade_negative_volume":len(negative_vol),
        "trade_present_but_candle_zero_volume":len(trade_with_zero_candle_vol),
        "candle_no_trade_with_quote_same_minute":len(set(unsupported_candles)&quote_minutes),
        "candle_no_trade_top_days":candle_without_trade_by_day.most_common(12),
        "positive_volume_no_trade_top_days":positive_without_trade_by_day.most_common(12),
        "positive_volume_no_trade_examples_utc":[
            {"minute":datetime.fromtimestamp(m*60,timezone.utc).isoformat(),
             "candle_base_volume":candle_volumes[m]}
            for m in positive_vol[:25]],
        "trade_present_but_candle_zero_volume_examples_utc":[
            datetime.fromtimestamp(m*60,timezone.utc).isoformat()
            for m in sorted(trade_with_zero_candle_vol)[:20]],
        "trade_minutes_missing_candle_examples_utc":[datetime.fromtimestamp(m*60,timezone.utc).isoformat() for m in extra_trades[:20]],
        "candle_without_trade_examples_utc":[datetime.fromtimestamp(m*60,timezone.utc).isoformat() for m in unsupported_candles[:20]],
        "window_utc":[datetime.fromtimestamp(m*60,timezone.utc).isoformat() for m in (start,end)],
        "interpretation_note":"A candle with zero volume and no matching raw trade is consistent with a no-trade candle, but does not prove complete capture. A positive-volume candle lacking raw trades requires investigation of archive coverage, timestamp mappings, and trade pagination. Market depth does not prove trades occurred."
    }
    destination=base/(symbol+"_CANDLE_TRADE_AUDIT.json")
    destination.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    print("AUDIT_SAVED",destination)
    return report

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--input",required=True)
    p.add_argument("--symbol",default="PAXG")
    a=p.parse_args()
    audit(Path(a.input).resolve(),a.symbol)
