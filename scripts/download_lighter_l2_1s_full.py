#!/usr/bin/env python3
"""Resumable, credit-budgeted 0xArchive Lighter XAU/PAXG 1-second L2 downloader.

Research data only; does not trade or backtest. Python 3.10+ stdlib only.
Rows preserved as provider JSON in per-chunk JSONL.GZ; metadata manifests give
counts, timestamp ranges, validation/coverage, SHA256 and request ledger.
"""
import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
BASE = "https://api.0xarchive.io/v1/lighter"
WINDOW_START = "2026-11-04T01:00:00Z"
WINDOW_END = "2026-11-19T00:00:00Z"  # exclusive; same as frozen 15d replay
SYMBOLS = ("XAU", "PAXG")


def parse_utc(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / (1000 if abs(value) > 1e11 else 1), UTC)
    if isinstance(value, str) and value.isdigit():
        return parse_utc(int(value))
    if not isinstance(value, str):
        raise ValueError("Unexpected timestamp type")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def name(d):
    return d.strftime("%Y%m%dT%H%M%SZ")


def get_json(url, api_key, timeout=90):
    """One HTTP attempt. Caller owns retry and credit ledger."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.0xarchive.io":
        raise ValueError("Refusing non-0xArchive API host")
    req = urllib.request.Request(url, headers={
        "X-API-Key": api_key,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "lighter-l2-1s-resumable/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(60_000_001)
        if len(raw) > 60_000_000:
            raise RuntimeError("API response >60 MB; reduce --chunk-minutes")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise ValueError("API returned failure; verify coverage, subscription and key")
    return payload


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(1024 * 1024), b""):
            h.update(buf)
    return h.hexdigest()


def append_ledger(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def ledger_attempts(path):
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def fetch_pages(start, end, symbol, args, key, ledger, used):
    endpoint = f"{BASE}/orderbook/{symbol}/history"
    cursor = None
    seen_cursors = set()
    rows = []
    request_ids = []
    page_count = 0
    while True:
        params = {"start": iso(start), "end": iso(end), "granularity": "1s",
                  "depth": args.depth, "limit": 1000}
        if cursor is not None:
            params["cursor"] = cursor
        url = endpoint + "?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            if used[0] >= args.max_credits:
                raise RuntimeError(f"CREDIT_CAP_REACHED: {used[0]} / {args.max_credits} requests; resume with a new cap only after checking balance")
            # Budget is conservative: count EVERY attempted API request, including retries.
            append_ledger(ledger, {"at_utc": iso(datetime.now(UTC)), "symbol": symbol,
                                   "window_start": iso(start), "window_end": iso(end),
                                   "attempt": attempt + 1, "page": page_count + 1})
            used[0] += 1
            try:
                payload = get_json(url, key, timeout=args.timeout)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 402, 403, 404, 422):
                    raise RuntimeError(f"HTTP {exc.code} on {symbol} {iso(start)}: check entitlement, symbol or API parameters") from None
                if exc.code not in (429, 500, 502, 503, 504) or attempt >= 3:
                    raise RuntimeError(f"HTTP {exc.code} on {symbol} {iso(start)}") from None
                retry_after = exc.headers.get("Retry-After", "")
                pause = min(15, float(retry_after)) if retry_after.replace(".", "", 1).isdigit() else min(8, 2 ** attempt)
                time.sleep(pause)
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt >= 3:
                    raise RuntimeError(f"Connection failure at {symbol} {iso(start)} ({type(exc).__name__})") from None
                time.sleep(min(8, 2 ** attempt))
            except (json.JSONDecodeError, ValueError, RuntimeError):
                raise
        page_count += 1
        if not isinstance(payload.get("data"), list):
            raise ValueError(f"Non-list API data on {symbol} {iso(start)}; not accepting as empty")
        data = payload["data"]
        if len(data) > 1000:
            raise ValueError("Received more than 1000 records per request; budget model no longer valid")
        rows.extend(data)
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            raise ValueError("API meta must be object")
        if meta.get("request_id"):
            request_ids.append(meta["request_id"])
        token = meta.get("next_cursor") or meta.get("nextCursor") or payload.get("next_cursor")
        more = meta.get("has_more", payload.get("has_more"))
        if more is True or (more is None and token):
            if not token or token in seen_cursors:
                raise RuntimeError("Incomplete or cyclic cursor pagination; window not saved")
            if page_count >= args.max_pages:
                raise RuntimeError("Unexpectedly many pages for 15-minute 1s window; window not saved")
            seen_cursors.add(token)
            cursor = token
            continue
        if more is None and len(data) >= 1000:
            raise RuntimeError("Full limit=1000 page but no pagination status; window not saved")
        break
    return rows, {"pages": page_count, "request_ids": request_ids}


def validate_rows(rows, start, end):
    """Validate and keep only rows in the half-open UTC chunk interval."""
    retained = []
    observed_seconds = set()
    out_of_window = 0
    last = None
    for row in rows:
        if not isinstance(row, dict) or "timestamp" not in row:
            raise ValueError("L2 row lacks timestamp object")
        ts = parse_utc(row["timestamp"])
        if ts < start or ts > end:
            raise ValueError(f"L2 row outside request: {iso(ts)} not within {iso(start)}..{iso(end)}")
        if ts == end:
            # End-inclusive backend behavior; do not double count boundary.
            out_of_window += 1
            continue
        if not isinstance(row.get("bids"), list) or not isinstance(row.get("asks"), list):
            raise ValueError("Unexpected L2 row schema (bids/asks missing or not arrays)")
        if last is not None and ts < last:
            raise ValueError("L2 timestamps not sorted; refuse to silently reorder source")
        last = ts
        sec = int(ts.timestamp())
        if sec in observed_seconds:
            raise ValueError(f"Duplicate second within granularity=1s: {iso(ts)}")
        observed_seconds.add(sec)
        retained.append(row)
    target = int((end-start).total_seconds())
    if target <= 0:
        raise ValueError("Invalid window")
    return retained, {"expected_seconds": target,
                      "observed_unique_seconds": len(observed_seconds),
                      "coverage_pct": round(100 * len(observed_seconds) / target, 3),
                      "excluded_end_boundary": out_of_window,
                      "first_row_ts": str(retained[0]["timestamp"]) if retained else None,
                      "last_row_ts": str(retained[-1]["timestamp"]) if retained else None}


def write_chunk(chunk, sidecar, rows, metadata):
    chunk.parent.mkdir(parents=True, exist_ok=True)
    tmp = chunk.with_name(chunk.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")
    os.replace(tmp, chunk)
    metadata["size_bytes"] = chunk.stat().st_size
    metadata["sha256"] = sha256_file(chunk)
    atomic_json(sidecar, metadata)


def verify_done(chunk, sidecar, expected):
    if not chunk.exists() or not sidecar.exists():
        return None
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise RuntimeError(f"Invalid existing sidecar: {sidecar}")
    for attr, val in expected.items():
        if meta.get(attr) != val:
            raise RuntimeError(f"Existing data has a different parameter {attr}: {sidecar}; use another --output-root")
    if chunk.stat().st_size != meta.get("size_bytes") or sha256_file(chunk) != meta.get("sha256"):
        raise RuntimeError(f"Corrupt/incomplete existing chunk: {chunk}; do not silently overwrite")
    return meta


def all_windows(start, end, minutes):
    step = timedelta(minutes=minutes)
    t = start
    while t < end:
        nxt = min(t + step, end)
        for symbol in SYMBOLS:
            yield symbol, t, nxt
        t = nxt


def csv_manifest(path, metas):
    fields = ["symbol", "start", "end", "row_count", "expected_seconds", "observed_unique_seconds",
              "coverage_pct", "pages", "size_bytes", "sha256", "file"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for m in metas:
            writer.writerow({k: m.get(k) for k in fields})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=WINDOW_START)
    parser.add_argument("--end", default=WINDOW_END)
    parser.add_argument("--chunk-minutes", type=int, default=15)
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--max-credits", type=int, default=4000,
                        help="Conservative request-attempt cap across runs, NOT authoritative provider balance")
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--min-free-gb", type=float, default=2.0)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--output-root", default="data/lighter_1s_20261122")
    parser.add_argument("--summary-zip", default="/mnt/c/Users/June-ASUS/Downloads/LIGHTER_1S_15D_DOWNLOAD_REPORT.zip")
    args = parser.parse_args(argv)
    start, end = parse_utc(args.start), parse_utc(args.end)
    if start >= end or (end - start).days > 30:
        parser.error("Invalid interval; maximum 30 days")
    if not 1 <= args.chunk_minutes <= 16 or not 1 <= args.depth <= 50:
        parser.error("--chunk-minutes 1..16 and --depth 1..50 required")
    if args.max_credits < 2 or args.timeout < 1:
        parser.error("Max credits must be >=2 and timeout >=1")
    key = os.getenv("OXARCHIVE_API_KEY", "").strip()
    if not key:
        parser.error("OXARCHIVE_API_KEY is missing (do not post the key into chat)")
    root = Path(args.output_root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / "request_attempts.jsonl"
    used = [ledger_attempts(ledger)]
    count_windows = math.ceil((end - start).total_seconds() / (args.chunk_minutes * 60))
    total_chunks = count_windows * len(SYMBOLS)
    min_expected = total_chunks  # normal 1s API and <=1000 returned rows
    print("PERIOD_UTC", iso(start), "to", iso(end), "exclusive", flush=True)
    print("CHUNKS", total_chunks, "MIN_REQUESTS", min_expected,
          "CREDIT_ATTEMPT_CAP", args.max_credits, "ALREADY_ATTEMPTED", used[0], flush=True)
    if min_expected > args.max_credits:
        raise RuntimeError("This chunk setting requires more requests than the credit cap; use 15m chunks")
    parameters = {"granularity": "1s", "depth": args.depth,
                  "chunk_minutes": args.chunk_minutes, "start": iso(start), "end": iso(end)}
    fingerprint = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()[:16]
    stamp = root / "configuration.json"
    if stamp.exists():
        old = json.loads(stamp.read_text(encoding="utf-8"))
        if old.get("fingerprint") != fingerprint:
            raise RuntimeError("Output folder belongs to a different date/depth/chunk-size job")
    else:
        atomic_json(stamp, {"fingerprint": fingerprint, "configuration": parameters,
                            "data_source": BASE, "notes": "Do not alter files; re-run to resume"})
    entries = []
    failures = []
    completed = 0
    existing = 0
    for symbol, lo, hi in all_windows(start, end, args.chunk_minutes):
        label = f"{name(lo)}__{name(hi)}"
        chunk = root / "l2_1s" / symbol / (label + ".jsonl.gz")
        sidecar = root / "l2_1s" / symbol / (label + ".meta.json")
        expected = {"symbol": symbol, "start": iso(lo), "end": iso(hi),
                    "granularity": "1s", "depth": args.depth}
        try:
            m = verify_done(chunk, sidecar, expected)
            if m:
                existing += 1
            else:
                if shutil.disk_usage(root).free < int(args.min_free_gb * (1024**3)):
                    raise RuntimeError("DISK_SPACE_LOW: free space below safety limit")
                rows, stats = fetch_pages(lo, hi, symbol, args, key, ledger, used)
                rows, quality = validate_rows(rows, lo, hi)
                if not rows and completed + existing < 2:
                    raise RuntimeError("Initial source sample empty; stop before spending monthly credits")
                m = {**expected, **quality, **stats, "row_count": len(rows),
                     "file": str(chunk), "saved_at_utc": iso(datetime.now(UTC))}
                write_chunk(chunk, sidecar, rows, m)
                completed += 1
            entries.append(m)
            done = completed + existing
            if done <= 2 or done % 50 == 0 or done == total_chunks:
                print("PROGRESS", done, "/", total_chunks,
                      "symbol", symbol, "chunk", label,
                      "rows", m["row_count"], "coverage%", m["coverage_pct"],
                      "credit_attempts", used[0], flush=True)
            if args.probe_only and done == 2:
                print("PROBE_ONLY: first XAU/PAXG samples ready; rerun without --probe-only", flush=True)
                break
        except (ValueError, OSError, RuntimeError) as exc:
            failures.append({"symbol": symbol, "start": iso(lo), "end": iso(hi),
                             "error": str(exc)})
            print("STOP_ERROR", symbol, iso(lo), str(exc), flush=True)
            break
    daily = defaultdict(lambda: {"chunks": 0, "rows": 0, "seconds": 0, "expected_seconds": 0})
    for m in entries:
        d = daily[(m["symbol"], m["start"][:10])]
        d["chunks"] += 1
        d["rows"] += m["row_count"]
        d["seconds"] += m["observed_unique_seconds"]
        d["expected_seconds"] += m["expected_seconds"]
    report = {"configuration": parameters,
              "note": "Raw 1s snapshots downloaded only; no trading simulation and no API entitlement guarantee",
              "credit_budget": {"cap_request_attempts": args.max_credits,
                                "attempts_in_ledger_total": used[0],
                                "billing": "Conservative minimum 1 credit per request attempt; actual balance/billing only provider knows"},
              "progress": {"total_chunks": total_chunks, "completed_chunks_found": len(entries),
                           "fresh_downloads_this_run": completed, "resumed_chunks": existing},
              "source_quality": {"empty_chunks": sum(e["row_count"] == 0 for e in entries),
                                 "sub_90pct_coverage_chunks": sum(e["coverage_pct"] < 90 for e in entries),
                                 "source_coverage_is_not_execution_proof": True},
              "daily": [{"symbol": k[0], "utc_date": k[1], **v,
                         "coverage_pct": round(100*v["seconds"]/v["expected_seconds"], 3)
                         if v["expected_seconds"] else None}
                        for k, v in sorted(daily.items())],
              "failures": failures,
              "complete": len(entries) == total_chunks and not failures}
    atomic_json(root / "download_summary.json", report)
    csv_manifest(root / "chunks_manifest.csv", entries)
    dest = Path(args.summary_zip)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for path in (stamp, root / "download_summary.json", root / "chunks_manifest.csv"):
            z.write(path, arcname=path.name)
    print("DATA_FOLDER", root.resolve(), flush=True)
    print("UPLOAD_REPORT", dest, flush=True)
    print("COMPLETE", report["complete"], "CHUNKS", len(entries), "/", total_chunks, flush=True)
    if failures:
        print("RESUME_AFTER_FIX: rerun the SAME command (completed verified chunks are skipped)", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
