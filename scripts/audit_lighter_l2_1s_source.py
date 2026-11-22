#!/usr/bin/env python3
"""Read-only verification of 1-second L2 data and systematic chunk gaps. No API calls."""
import argparse,csv,gzip,hashlib,json,zipfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
UTC=timezone.utc
EVENTS=("2026-11-04T01:04:00Z","2026-11-04T05:04:00Z",
        "2026-11-12T08:20:00Z","2026-11-14T20:55:00Z")
def dt(s):
    if isinstance(s,(int,float)) or (isinstance(s,str) and s.isdigit()):
        n=float(s)
        return datetime.fromtimestamp(n/(1000 if n>1e11 else 1),UTC)
    return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(UTC)
def stamp(d):return d.isoformat().replace("+00:00","Z")
def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1<<20),b""):h.update(block)
    return h.hexdigest()
def check_one(root,row):
    p=root/"l2_1s"/row["symbol"]/Path(row["file"]).name
    if sha(p)!=row["sha256"]:raise RuntimeError("Chunk checksum mismatch: "+str(p))
    start,end=dt(row["start"]),dt(row["end"])
    visited=set();first=None;last=None;unique=set();previous=None;unchanged=0;n=0
    with gzip.open(p,"rt",encoding="utf8") as f:
        for line in f:
            item=json.loads(line);t=dt(item["timestamp"])
            if not start<=t<end:raise RuntimeError("Timestamp outside chunk: "+stamp(t))
            sec=int(t.timestamp())
            if sec in visited:raise RuntimeError("Duplicate timestamp second: "+stamp(t))
            visited.add(sec)
            if first is None:first=t
            last=t
            bids=item.get("bids");asks=item.get("asks")
            if not isinstance(bids,list) or not isinstance(asks,list):
                raise RuntimeError("Unexpected L2 book schema")
            top=json.dumps([bids[:1],asks[:1]],separators=(",",":"))
            unique.add(top)
            if previous==top:unchanged+=1
            previous=top;n+=1
    expected=int((end-start).total_seconds())
    absent=[stamp(start+timedelta(seconds=i)) for i in range(expected)
            if int(start.timestamp())+i not in visited]
    return {"symbol":row["symbol"],"chunk_start":row["start"],
            "expected":expected,"rows":n,
            "manifest_count_matches":n==int(row["row_count"]),
            "first":stamp(first) if first else None,
            "last":stamp(last) if last else None,
            "missing_seconds":absent,
            "unique_top_of_book_states":len(unique),
            "unchanged_consecutive_fraction":round(unchanged/(n-1),4) if n>1 else None}
def audit(root):
    with (root/"chunks_manifest.csv").open(newline="",encoding="utf8") as f:
        rows=list(csv.DictReader(f))
    selected={}
    for sym in ("XAU","PAXG"):
        symbol_rows=[r for r in rows if r["symbol"]==sym]
        if not symbol_rows:raise RuntimeError("Missing "+sym+" in manifest")
        for row in (symbol_rows[0],symbol_rows[-1]):
            selected[(sym,row["start"])]=row
        for event in EVENTS:
            moment=dt(event)
            found=next((r for r in symbol_rows
                       if dt(r["start"])<=moment<dt(r["end"])),None)
            if found is None:raise RuntimeError("No data for "+sym+" at "+event)
            selected[(sym,found["start"])]=found
    items=[check_one(root,row) for _,row in sorted(selected.items())]
    return {"status":"PASS" if all(v["manifest_count_matches"] for v in items) else "FAIL",
            "checked_chunks":len(items),
            "every_checked_chunk_lacks_exactly_one_second":
                all(len(v["missing_seconds"])==1 for v in items),
            "samples":items,
            "scope":"Spot-check only; no trading/filled maker order verification"}
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",default="data/lighter_1s_20261122")
    p.add_argument("--output",default="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_1S_BOUNDARY_AUDIT.zip")
    a=p.parse_args()
    result=audit(Path(a.root))
    dest=Path(a.output);dest.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(dest,"w",compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("boundary_audit.json",json.dumps(result,indent=2))
    print("STATUS",result["status"],"SAMPLED",result["checked_chunks"])
    for x in result["samples"]:
        print(x["symbol"],x["chunk_start"],"missing",",".join(x["missing_seconds"]),
              "unique_books",x["unique_top_of_book_states"])
    print("UPLOAD",dest)
if __name__=="__main__":main()
