#!/usr/bin/env python3
"""Download NEW historical Lighter market data from 0xArchive. No live orders.

Private credentials are read ONLY from the OXARCHIVE_API_KEY environment variable.
Raw L2 levels are saved in gzip JSONL; the SQLite quotes table holds best level only.
"""
import argparse
import csv
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.0xarchive.io"
KINDS = ("book", "candles", "funding", "trades")
UTC = timezone.utc

class HistoryWindowExceeded(RuntimeError):
    """The free rolling retention boundary is newer than the requested start."""
    def __init__(self, earliest_ms, detail):
        self.earliest_ms=earliest_ms
        super().__init__(detail)


def timestamp(value):
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    if isinstance(value, str) and value.isdigit():
        return timestamp(int(value))
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()*1000)

def row_time(row):
    for k in ("timestamp", "ts_ms", "time", "open_time", "t"):
        if row.get(k) is not None:
            return timestamp(row[k])
    raise ValueError("timestamp missing in record")

def get(path, params, key):
    url = API + path + "?" + urlencode(params)
    for attempt in range(5):
        req = Request(url, headers={"X-API-Key": key, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=90) as response:
                obj = json.load(response)
            if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
                raise ValueError("Unexpected API data shape (only snapshot mode supported)")
            return obj
        except HTTPError as e:
            message = e.read(4096).decode("utf-8", "replace")
            if e.code == 403:
                try:
                    payload=json.loads(message)
                except (ValueError,TypeError):
                    payload={}
                if payload.get("error_code")=="history_window_exceeded":
                    match=re.search(r"earliest allowed timestamp\s+([0-9TZ:.+\-]+)",payload.get("error",""))
                    if not match:
                        raise RuntimeError("History boundary error without a parsable timestamp: "+message) from None
                    raise HistoryWindowExceeded(timestamp(match.group(1)),message) from None
            if e.code not in (429,500,502,503,504) or attempt == 4:
                raise RuntimeError(f"HTTP {e.code} for {path}: {message}") from None
            time.sleep(min(15,2**attempt))
        except (URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(min(15,2**attempt))
    raise RuntimeError("Retries exhausted")

def route(kind, symbol):
    return "/v1/lighter/" + {
        "book": f"orderbook/{symbol}/history",
        "candles": f"candles/{symbol}",
        "funding": f"funding/{symbol}",
        "trades": f"trades/{symbol}",
    }[kind]

def fetch_pages(kind, symbol, begin, end, key, granularity, depth):
    params = {"start":begin, "end":end, "limit":1000}
    if kind == "book": params.update(granularity=granularity,depth=depth)
    if kind == "candles": params["interval"] = "1m"
    seen=set()
    while True:
        packet=get(route(kind,symbol),params,key)
        yield packet["data"]
        meta=packet.get("meta") or {}
        cursor=meta.get("next_cursor")
        if cursor is None and not meta.get("has_more"):
            break
        if cursor is None or str(cursor) in seen:
            raise RuntimeError("Unexpected or repeated pagination cursor")
        seen.add(str(cursor))
        params["cursor"]=cursor

def level(p):
    if isinstance(p,dict):
        price=p.get("px",p.get("price"))
        size=p.get("sz",p.get("size",p.get("qty",p.get("quantity"))))
    elif isinstance(p,(list,tuple)) and len(p)>=2:
        price,size=p[:2]
    else:
        raise ValueError("Unknown L2 level shape")
    return float(price),float(size)

def top_quote(symbol, row):
    bids=[level(p) for p in row["bids"]]
    asks=[level(p) for p in row["asks"]]
    if not bids or not asks:return None
    bid,bs=max(bids,key=lambda x:x[0])
    ask,az=min(asks,key=lambda x:x[0])
    if bid<=0 or ask<bid or bs<0 or az<0:
        raise ValueError("Invalid/crossed quote")
    return (symbol,row_time(row),bid,ask,bs,az)

def candle(symbol,row):
    def num(*names):
        for k in names:
            if row.get(k) is not None:return float(row[k])
        raise ValueError("missing candle field "+repr(names))
    op=num("open","o"); hi=num("high","h");lo=num("low","l");cl=num("close","c")
    vol=None
    for v in ("base_volume","volume","volume_base","v"):
        if row.get(v) is not None:
            vol=float(row[v]);break
    if min(op,hi,lo,cl)<=0 or hi<max(op,cl) or lo>min(op,cl):
        raise ValueError("Bad OHLC")
    return (symbol,row_time(row),op,hi,lo,cl,vol)

def init(db):
    db.execute("create table if not exists candles(symbol text,ts_ms integer,open real,high real,low real,close real,base_volume real,primary key(symbol,ts_ms))")
    db.execute("create table if not exists quotes(symbol text,ts_ms integer,bid real,ask real,bid_size real,ask_size real,primary key(symbol,ts_ms))")
    db.execute("create table if not exists funding(symbol text,ts_ms integer,rate text,raw_json text,primary key(symbol,ts_ms))")
    db.execute("create table if not exists chunks(symbol text,kind text,begin_ms integer,end_ms integer,rows_count integer,primary key(symbol,kind,begin_ms,end_ms))")
    db.execute("create table if not exists history_gaps(symbol text,kind text,requested_start_ms integer,actual_start_ms integer,end_ms integer,reason text,primary key(symbol,kind,requested_start_ms,end_ms))")
    db.commit()

def collect(db,base,kind,symbol,start,end,key,granularity,depth):
    folder=base/kind/symbol;folder.mkdir(parents=True,exist_ok=True)
    final=folder/f"{start}_{end}.jsonl.gz"
    done=db.execute("select 1 from chunks where symbol=? and kind=? and begin_ms=? and end_ms=?",(symbol,kind,start,end)).fetchone()
    if done and final.is_file():
        print(f"{symbol} {kind}: SKIP already downloaded")
        return
    if done and not final.is_file():
        # Recover a previous crash between database commit and writing the raw file.
        db.execute("delete from chunks where symbol=? and kind=? and begin_ms=? and end_ms=?",(symbol,kind,start,end))
        db.execute("delete from history_gaps where symbol=? and kind=? and requested_start_ms=? and end_ms=?",(symbol,kind,start,end))
        db.commit()
    tmp=folder/f"{start}_{end}.partial"
    rows=[];count=0
    effective_start=start
    try:
        # The free 30-day retention boundary moves forward while the download runs.
        # Adapt each affected chunk to the API's precise cutoff, preserving prior chunks.
        for boundary_retry in range(6):
            rows=[];count=0
            try:
                with gzip.open(tmp,"wt",encoding="utf-8") as out:
                    for batch in fetch_pages(kind,symbol,effective_start,end,key,granularity,depth):
                        count+=len(batch)
                        for item in batch:
                            out.write(json.dumps(item,separators=(",",":"),ensure_ascii=False)+"\n")
                            if kind in ("book","candles","funding"):rows.append(item)
                break
            except HistoryWindowExceeded as exc:
                new_start=max(effective_start+1000,exc.earliest_ms+90000)
                if new_start>=end:
                    effective_start=end
                    with gzip.open(tmp,"wt",encoding="utf-8"):
                        pass
                    print(f"{symbol} {kind}: HISTORY SKIP {start}..{end} (older than free retention)",flush=True)
                    break
                effective_start=new_start
                print(f"{symbol} {kind}: HISTORY CLIPPED to {datetime.fromtimestamp(effective_start/1000, UTC).isoformat()}; earlier records unavailable on Free",flush=True)
        else:
            raise RuntimeError(f"{symbol} {kind}: moving history boundary exceeded retry budget")
        # Ensure durable source bytes exist before marking the database chunk complete.
        # If the SQLite transaction fails, the next run safely fetches again.
        tmp.replace(final)
        with db:
            for item in rows:
                if kind=="book":
                    q=top_quote(symbol,item)
                    if q:db.execute("insert or replace into quotes values(?,?,?,?,?,?)",q)
                if kind=="candles":
                    db.execute("insert or replace into candles values(?,?,?,?,?,?,?)",candle(symbol,item))
                if kind=="funding":
                    rate=item.get("funding_rate",item.get("rate"))
                    db.execute("insert or replace into funding values(?,?,?,?)",(symbol,row_time(item),str(rate) if rate is not None else None,json.dumps(item)))
            db.execute("insert into chunks values(?,?,?,?,?)",(symbol,kind,start,end,count))
            if effective_start>start:
                db.execute("insert or replace into history_gaps values(?,?,?,?,?,?)",
                           (symbol,kind,start,effective_start,end,"free_plan_rolling_retention"))
        print(f"{symbol} {kind}: DONE {count:,} records",flush=True)
    finally:
        if tmp.exists():tmp.unlink()

def main():
    p=argparse.ArgumentParser(description="0xArchive Lighter history; requires OXARCHIVE_API_KEY")
    p.add_argument("--days",type=float,default=0.04)
    p.add_argument("--symbols",nargs="+",default=["XAU","PAXG"])
    p.add_argument("--granularity",choices=["checkpoint","30s","10s","1s"],default="checkpoint")
    p.add_argument("--depth",type=int,default=20)
    p.add_argument("--skip",nargs="*",choices=KINDS,default=[])
    p.add_argument("--output",default="data/lighter_probe")
    p.add_argument("--chunk-hours",type=int,default=6)
    a=p.parse_args()
    key=os.getenv("OXARCHIVE_API_KEY","").strip()
    if not key:sys.exit("OXARCHIVE_API_KEY missing (do not share it in chat)")
    if not (0<a.days<=30 and a.depth>0 and a.chunk_hours>0):
        sys.exit("Invalid arguments")
    # Freeze the requested window on first run. Without this, re-running after
    # interruption would shift all chunk boundaries and download everything twice.
    base=Path(a.output);base.mkdir(parents=True,exist_ok=True)
    range_file=base/"DOWNLOAD_RANGE.json"
    options={"days":a.days,"symbols":a.symbols,"granularity":a.granularity,
             "depth":a.depth,"skip":a.skip,"chunk_hours":a.chunk_hours}
    if range_file.is_file():
        saved=json.loads(range_file.read_text())
        if saved["options"]!=options:
            sys.exit("Existing output uses different options; reuse the original options or a new --output")
        begin_ms=int(saved["begin_ms"])
        end_ms=int(saved["end_ms"])
        print("RESUME frozen interval from",range_file,flush=True)
    else:
        now=datetime.now(UTC)
        end=(now-timedelta(minutes=30)).replace(second=0,microsecond=0)
        begin=end-timedelta(days=a.days)+timedelta(minutes=5)
        if begin>=end:
            sys.exit("Range too short; try --days 0.04")
        begin_ms=timestamp(begin.isoformat());end_ms=timestamp(end.isoformat())
        range_file.write_text(json.dumps({"begin_ms":begin_ms,"end_ms":end_ms,
                                         "options":options},indent=2))
    begin=datetime.fromtimestamp(begin_ms/1000,UTC)
    end=datetime.fromtimestamp(end_ms/1000,UTC)
    print("Requested UTC:",begin.isoformat(),"to",end.isoformat(),flush=True)
    db=sqlite3.connect(base/"lighter_marketdata.sqlite3");init(db)
    try:
        for sym in a.symbols:
            for kind in KINDS:
                if kind in a.skip:continue
                for b in range(begin_ms,end_ms,a.chunk_hours*3600000):
                    collect(db,base,kind,sym,b,min(end_ms,b+a.chunk_hours*3600000),key,a.granularity,a.depth)
        with (base/"historical_bid_ask.csv").open("w",newline="",encoding="utf8") as out:
            w=csv.writer(out);w.writerow(["ts_ms","symbol","bid","ask","bid_size","ask_size"])
            w.writerows(db.execute("select ts_ms,symbol,bid,ask,bid_size,ask_size from quotes order by ts_ms,symbol"))
        summary={}
        for table in ("quotes","candles","funding"):
            summary[table]={s:db.execute(f"select count(*) from {table} where symbol=?",(s,)).fetchone()[0] for s in a.symbols}
        gaps=[dict(zip(("symbol","kind","requested_start_ms","actual_start_ms","end_ms","reason"),r))
              for r in db.execute("select symbol,kind,requested_start_ms,actual_start_ms,end_ms,reason from history_gaps order by requested_start_ms")]
        (base/"QUALITY_REPORT.json").write_text(json.dumps({"requested_utc":[begin.isoformat(),end.isoformat()],"counts":summary,"history_gaps":gaps},indent=2))
        print("COMPLETE",base.resolve(),json.dumps(summary),flush=True)
    finally:db.close()

if __name__=="__main__":
    try:main()
    except (ValueError,RuntimeError,HTTPError,URLError,sqlite3.Error,KeyError) as e:
        sys.exit(f"DOWNLOAD FAILED: {e}")
