#!/usr/bin/env python3
"""Package small review ZIP of historical Lighter data, preserving full L2 only locally."""
import argparse, csv, gzip, io, json, sqlite3, zipfile
from datetime import datetime, timezone
from pathlib import Path

def coverage(db, table):
    return [{"symbol":r[0],"rows":r[1],
             "first_utc":datetime.fromtimestamp(r[2]/1000,timezone.utc).isoformat() if r[2]!=None else None,
             "last_utc":datetime.fromtimestamp(r[3]/1000,timezone.utc).isoformat() if r[3]!=None else None}
            for r in db.execute(f"select symbol,count(*),min(ts_ms),max(ts_ms) from {table} group by symbol")]

def sample_csv(db, query):
    cursor=db.execute(query);s=io.StringIO();w=csv.writer(s)
    w.writerow([col[0] for col in cursor.description])
    w.writerows(cursor.fetchall());return s.getvalue()

def package(source, destination):
    dbpath=source/"lighter_marketdata.sqlite3"
    if not dbpath.is_file():raise FileNotFoundError(dbpath)
    with sqlite3.connect(f"file:{dbpath}?mode=ro",uri=True) as db:
        report={name:coverage(db,name) for name in ("quotes","candles","funding")}
        report["chunks"]=[dict(zip(("symbol","kind","chunks","rows"),r)) for r in db.execute(
            "select symbol,kind,count(*),sum(rows_count) from chunks group by symbol,kind order by symbol,kind")]
        report["history_gaps"]=[dict(zip(("symbol","kind","requested_start_ms","actual_start_ms","end_ms","reason"),r)) for r in db.execute("select symbol,kind,requested_start_ms,actual_start_ms,end_ms,reason from history_gaps order by requested_start_ms")] if db.execute("select 1 from sqlite_master where type='table' and name='history_gaps'").fetchone() else []
        quotes=sample_csv(db,"select * from quotes order by ts_ms desc,symbol limit 200")
        candles=sample_csv(db,"select * from candles order by ts_ms desc,symbol limit 50")
        funding=sample_csv(db,"select * from funding order by ts_ms desc,symbol limit 20")
    book_sample=[]
    # A balanced sample from both assets is necessary for independent L2 checks.
    for symbol in ("XAU","PAXG"):
        for file in sorted((source/"book"/symbol).glob("*.jsonl.gz"))[:2]:
            with gzip.open(file,"rt",encoding="utf8") as data:
                for i,line in enumerate(data):
                    if i>=3:break
                    book_sample.append(line.rstrip())
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("data_coverage.json",json.dumps(report,indent=2))
        z.writestr("sample_quotes.csv",quotes)
        z.writestr("sample_candles.csv",candles)
        z.writestr("sample_funding.csv",funding)
        z.writestr("sample_l2_raw.jsonl","\n".join(book_sample)+"\n")
        for fn in ("QUALITY_REPORT.json","DOWNLOAD_RANGE.json"):
            if (source/fn).exists():z.write(source/fn,fn)
    print("REVIEW_ZIP",destination)
    print("COVERAGE",json.dumps(report))
    return report

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--input",required=True)
    p.add_argument("--output",required=True)
    a=p.parse_args()
    package(Path(a.input),Path(a.output))
