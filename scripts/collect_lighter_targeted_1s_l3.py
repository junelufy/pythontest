#!/usr/bin/env python3
"""Read-only targeted 0xArchive evidence collection (Lighter XAU/PAXG).

Downloads 1-second L2 for short, predefined incident windows; PAXG public
trades; and point-in-time PAXG L3 samples. Preserves API JSON unmodified in
a single ZIP. Does NOT simulate orders, claim maker fills, or change strategy.

API key in OXARCHIVE_API_KEY only: never printed, saved, or committed.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://api.0xarchive.io/v1/lighter"
UTC = timezone.utc
CENTERS = {
    "nov04_0104_maker": "2026-11-04T01:04:00Z",
    "nov04_0504_maker": "2026-11-04T05:04:00Z",
    "nov12_0820_through": "2026-11-12T08:20:00Z",
    "nov14_2055_stale": "2026-11-14T20:55:00Z",
}


def fmt(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text):
    return datetime.fromisoformat(text.replace("Z","+00:00")).astimezone(UTC)


def url_for(path, params):
    return BASE + path + "?" + urllib.parse.urlencode(params)


def get_json(url, api_key):
    # Only this fixed trusted origin receives the key.
    if urllib.parse.urlsplit(url).netloc != "api.0xarchive.io":
        raise ValueError("Unexpected API host")
    req = urllib.request.Request(
        url, headers={"X-API-Key": api_key, "Accept": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                raw=response.read()
            payload=json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
            if payload.get("error") or payload.get("error_code"):
                raise ValueError("API error: "+str(payload.get("error_code",payload.get("error"))))
            return payload
        except urllib.error.HTTPError as error:
            # No secret/API key ever appears in this error.
            if error.code in (429,500,502,503,504) and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"HTTP {error.code}; verify account plan, coverage and quota") from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"Network error: {type(error).__name__}") from error
    raise AssertionError("unreachable")


def page_data(payload):
    data=payload.get("data")
    if not isinstance(data,(list,dict)):
        raise ValueError("Unexpected response: data must be list or object")
    return data


def next_cursor(payload):
    # No guesswork: support common cursor locations, fail on unrecognized has_more.
    meta=payload.get("meta") or {}
    if not isinstance(meta, dict):meta={}
    return (payload.get("next_cursor") or payload.get("nextCursor")
            or meta.get("next_cursor") or meta.get("nextCursor")
            or payload.get("cursor") or meta.get("cursor"))


def collect_pages(path, params, api_key, name, files, audit):
    cursor=None
    seen=set()
    all_count=0
    pages=0
    while True:
        p=dict(params)
        if cursor:p["cursor"]=cursor
        url=url_for(path,p)
        payload=get_json(url,api_key)
        data=page_data(payload)
        pages+=1
        count=len(data) if isinstance(data,list) else 1
        all_count+=count
        # Preserve every response and its query params, without auth headers.
        files[f"{name}/page_{pages:03d}.json"]=json.dumps(
            {"query":p,"response":payload},ensure_ascii=False,separators=(",",":")
        )
        if pages>=40:
            raise RuntimeError("Pagination exceeds 40 pages for "+name)
        token=next_cursor(payload)
        has_more=bool(payload.get("has_more", (payload.get("meta") or {}).get("has_more",False)))
        if token and token not in seen:
            seen.add(token)
            cursor=token
            continue
        if has_more:
            raise RuntimeError("Pagination indicates more records but no new cursor for "+name)
        break
    audit.append({"name":name,"records":all_count,"pages":pages})
    return all_count


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes-each-side",type=int,default=2)
    ap.add_argument("--output",default="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_TARGETED_L2_1S_L3_20261122.zip")
    ap.add_argument("--skip-l3",action="store_true")
    args=ap.parse_args(argv)
    if not 1<=args.minutes_each_side<=5:
        ap.error("Minutes must be 1..5")
    api_key=os.environ.get("OXARCHIVE_API_KEY","").strip()
    if not api_key:
        ap.error("Set OXARCHIVE_API_KEY in your shell (never paste it into chat)")
    content={}
    audit=[]
    errors=[]
    for name,stamp in CENTERS.items():
        center=parse(stamp)
        start=center-timedelta(minutes=args.minutes_each_side)
        end=center+timedelta(minutes=args.minutes_each_side)
        print("WINDOW",name,fmt(start),fmt(end),flush=True)
        # Request one minute at a time: 1s × 60 records under limit=1000.
        for minute in range(args.minutes_each_side*2):
            a=start+timedelta(minutes=minute)
            b=a+timedelta(minutes=1)
            for symbol in ("XAU","PAXG"):
                key=f"{name}/l2_1s_{symbol}_{minute:02d}"
                try:
                    count=collect_pages(
                        f"/orderbook/{symbol}/history",
                        {"start":fmt(a),"end":fmt(b),
                         "granularity":"1s","depth":20,"limit":1000},
                        api_key,key,content,audit
                    )
                    if count==0:errors.append(f"No {symbol} L2 at {fmt(a)}")
                except Exception as exc:
                    errors.append(f"{key}: {exc}")
                    print("ERROR",key,exc,flush=True)
        try:
            collect_pages(
                "/trades/PAXG",
                {"start":fmt(start),"end":fmt(end),"limit":1000},
                api_key,f"{name}/paxg_trades",content,audit
            )
        except Exception as exc:
            errors.append(f"{name}/paxg_trades: {exc}")
            print("ERROR",name,"trades",exc,flush=True)
        if not args.skip_l3:
            for offset in (-60,0,60):
                moment=fmt(center+timedelta(seconds=offset))
                key=f"{name}/l3_PAXG_{offset:+03d}s"
                try:
                    # Point-in-time snapshot, not full order event history.
                    collect_pages(
                        "/l3orderbook/PAXG",
                        {"timestamp":moment,"depth":250},
                        api_key,key,content,audit
                    )
                except Exception as exc:
                    errors.append(f"{key}: {exc}")
                    print("WARN_L3",key,exc,flush=True)
    summary={
        "study":"Targeted high resolution historical source evidence; no maker order execution",
        "centers_utc":CENTERS,
        "lookaround_minutes_each_side":args.minutes_each_side,
        "source":"0xArchive REST Lighter mainnet",
        "l2_granularity_requested":"1s",
        "note":"API availability does NOT establish true 1s coverage; check response timestamps and gaps",
        "crucial_limit":"Sampling 4 short windows cannot prove an 8-day pending maker order survived every spread reversion",
        "maker_fill_status":"UNKNOWN: no user maker order id or private acknowledgements",
        "audit":audit,"errors":errors,
    }
    content["SUMMARY.json"]=json.dumps(summary,indent=2,ensure_ascii=False)
    dest=Path(args.output).expanduser()
    dest.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(dest,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,raw in sorted(content.items()):
            z.writestr(name,raw)
    print("ZIP",dest,flush=True)
    print("CALLS_WITH_DATA",len(audit),"ISSUES",len(errors),flush=True)
    if errors:
        print("PARTIAL_RESULT: inspect SUMMARY.json; no blank data may be treated as zero market activity",flush=True)
        return 2
    print("SOURCE_EVIDENCE_READY: upload ZIP for independent analysis",flush=True)
    return 0


if __name__=="__main__":
    sys.exit(main())
