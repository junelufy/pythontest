import gzip
import json
from datetime import datetime, timezone

import pytest

from scripts.calibrate_lighter_spread_5d import (
    DEFAULT_START, ms, make_calibration, read_calibration_books,
    histogram, summary_stats,
)

def fixture_books(root, when):
    start = ms(when)
    cutoff = start + 5*86400_000
    for sym, b in (("XAU", 4030), ("PAXG", 4000)):
        directory = root/"book"/sym
        directory.mkdir(parents=True)
        first=directory/f"{start-60000}_{start+600000}.jsonl.gz"
        with gzip.open(first,"wt",encoding="utf8") as out:
            for minute in range(3):
                ts=start+minute*60000-10000
                item={"timestamp":datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),
                      "bids":[[str(b),"2"]],"asks":[[str(b+1),"2"]]}
                out.write(json.dumps(item)+"\n")
        # A corrupt gzip file in the future must not be read by calibration at all
        (directory/f"{cutoff}_{cutoff+21600000}.jsonl.gz").write_bytes(b"NOT_A_VALID_GZIP")
    return start, cutoff


def test_calibration_5days_only_and_no_held_out_data_access(tmp_path):
    root=tmp_path/"dataset"
    start,cutoff=fixture_books(root,DEFAULT_START)
    times,books=read_calibration_books(root,"XAU",start,cutoff)
    assert len(books)==3
    assert max(times)<cutoff
    report=make_calibration(root,tmp_path/"out",start,5,[0.5],120,1)
    r=report["by_order_quantity"]["0.5"]
    assert report["calibration_start_inclusive_utc"]=="2026-10-23T01:00:00+00:00"
    assert report["calibration_end_exclusive_utc"]=="2026-10-28T01:00:00+00:00"
    assert report["reserved_out_of_sample_start_inclusive_utc"]==report["calibration_end_exclusive_utc"]
    assert report["reserved_out_of_sample_end_exclusive_utc"]=="2026-11-19T00:00:00+00:00"
    assert report["expected_sample_ticks"]==7200
    assert report["paired_fresh_snapshot_ticks"]==3
    assert r["maker_signal_xau_sell_vwap_minus_paxg_best_bid"]["min"]==30
    assert r["maker_signal_xau_sell_vwap_minus_paxg_best_bid"]["max"]==30
    assert r["taker_signal_xau_sell_vwap_minus_paxg_buy_vwap"]["min"]==29
    assert (tmp_path/"out"/"calibration_5d_summary.json").exists()
    assert (tmp_path/"out"/"calibration_5d_spread_samples.csv").exists()


def test_signed_spread_histogram_and_quantiles():
    buckets=histogram([-5.2,-5.0,-0.1,0.0,1.4,1.9],1)
    assert sum(r["minutes"] for r in buckets)==6
    assert buckets[0]["lower_usd_per_oz"]==-6
    assert summary_stats([-5.,3.,27.])["min"]==-5
    assert summary_stats([-5.,3.,27.])["max"]==27
    assert summary_stats([])["n"]==0
