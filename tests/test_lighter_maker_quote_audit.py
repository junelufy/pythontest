import csv
import gzip
import json
from pathlib import Path

from scripts.analyze_lighter_spread_feasibility import Book, iso, ms
from scripts.audit_lighter_forward_maker_15d import (
    evidence_for, load_candidates, parse_trade, quote_info, run,
    price_within_book_side,
)
from scripts.forwardtest_lighter_spread_grid_15d import TEST_START, TEST_END


def test_parse_public_trades_and_unsupported_schema():
    a=parse_trade({"timestamp":"2026-11-14T20:55:34.000Z",
                   "price":"3817.36","size":"0.8","side":"buy","trade_id":"123"})
    assert a["price"]==3817.36
    assert a["size"]==0.8
    assert a["side"]=="buy"
    assert a["id"]=="123"
    b=parse_trade({"ts_ms":ms("2026-11-14T20:55:34Z"),
                   "px":3817.36,"sz":.6})
    assert b["price"]==3817.36
    assert parse_trade({"unknown_key":"unsupported"}) is None


def test_quote_skew_and_maker_print_evidence_long_short():
    tick=ms("2026-11-14T20:55:00Z")
    x=Book(tick-14000,((3804.,2.),),((3805.,2.),))
    p=Book(tick-28000,((3815.,2.),),((3817.36,3.),))
    q=quote_info((tick,x,p))
    assert q["snapshot_skew_s"]==14
    assert q["source_skew_gt_10s"]
    prints=[
        {"ts_ms":tick+13000,"price":3817.30,"size":.3,"side":"buy"},
        {"ts_ms":tick+33000,"price":3817.40,"size":.2,"side":"buy"},
        {"ts_ms":tick+67000,"price":3817.45,"size":.4,"side":"buy"},
    ]
    candidates=[
        {"entry_ms":tick,"direction":"long","paxg_limit_from_replay":3817.36},
        {"entry_ms":tick,"direction":"short","paxg_limit_from_replay":3815.0},
    ]
    out=evidence_for(candidates[0],p,[r["ts_ms"] for r in prints],prints)
    assert out["eligible_prints_first_60s"]==1
    assert out["eligible_prints_first_5m"]==2
    assert out["eligible_size_first_60s_oz"]==.2
    assert out["existing_visible_size_at_price_oz"]==3.0
    assert out["maker_price_matches_replay"]
    sell=evidence_for(candidates[1],p,[r["ts_ms"] for r in prints],prints)
    assert sell["eligible_prints_first_60s"]==0
    assert sell["public_print_instant_fill_supported"].startswith("NO_PUBLIC")


def test_report_orders_fee_dedup_and_readonly_forensic_files(tmp_path):
    start=ms("2026-11-14T20:55:00Z")
    root=tmp_path/"data"
    old=root/"FORWARD_TP2_TP3_15D"
    for sub in ("paired_age120_skew120","paired_age120_skew10"):
        d=old/sub
        d.mkdir(parents=True)
        with (d/"trades_all_scenarios.csv").open("w",newline="") as out:
            rows=[{
                "entry_utc":iso(start),"exit_utc":iso(start+60000),
                "direction":"long","grid":"P25","quantity_oz":"0.5",
                "entry_p":"3817.36","mode":"maker_assumed",
                "fee_profile":f,"gross_usd_oz":"19.675128",
                "tp_gross_usd_oz":tp
            } for f in ("standard","plus") for tp in ("2.0","3.0")]
            w=csv.DictWriter(out,fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        (d/"terminal_open_positions.csv").write_text(
            "entry_utc,marked_at_utc,direction,grid,qty_oz_per_grid,mode,fee_profile\n",
            encoding="utf8")
    assert len(load_candidates(old))==2
    for sym,base in (("XAU",3804.),("PAXG",3815.)):
        folder=root/"book"/sym
        folder.mkdir(parents=True)
        with gzip.open(folder/f"{start-120000}_{start+120000}.jsonl.gz","wt") as out:
            for offset in (-20000,40000):
                t=start+offset
                if sym=="PAXG":
                    bid,ask=3815.,3817.36
                else:
                    bid,ask=3804.,3805.
                out.write(json.dumps({
                    "timestamp":iso(t),"bids":[[str(bid),"3"]],
                    "asks":[[str(ask),"3"]]
                })+"\n")
    folder=root/"trades"/"PAXG"
    folder.mkdir(parents=True)
    with gzip.open(folder/f"{TEST_START}_{TEST_END}.jsonl.gz","wt") as out:
        out.write(json.dumps({
            "timestamp":iso(start+30000),"price":3817.36,"size":.6,
            "side":"buy","trade_id":"A"
        })+"\n")
    results=run(root,old,tmp_path/"audit")
    assert results["coverage"]["paired_found"]==2
    assert results["coverage"]["eligible_prints_60s"]==2
    assert len(results["incident_20261114_2055_entries"])==2
    assert (tmp_path/"audit"/"maker_quote_forensic_summary.json").exists()
    assert (tmp_path/"audit"/"incident_20261114_2055_quotes.csv").exists()
    assert (tmp_path/"audit"/"all_maker_entry_evidence.csv").exists()


def test_replay_price_book_side_range_validation():
    b=Book(TEST_START,((4000.,0.5),(3999.,2.)),((4001.,0.5),(4002.,2.)))
    assert price_within_book_side(b,"buy",4001.3) is True
    assert price_within_book_side(b,"sell",3999.5) is True
    assert price_within_book_side(b,"buy",4000.) is False
    assert price_within_book_side(b,"sell",4002.) is False
    assert price_within_book_side(b,"buy",None) is None
