"""Offline regression tests for 1-second frozen-maker TP2/TP3 historical replay."""
import gzip
import json
import math
from collections import Counter
from pathlib import Path

import pytest

from scripts.analyze_lighter_spread_feasibility import Book, ms
from scripts.replay_lighter_causal_maker_first_15d import Tape, run_scenario
from scripts.replay_lighter_1s_frozen_tp2_tp3 import (
    fees_for_trade,derive_fee_profiles,iter_books,merge_timeline
)

T=ms("2026-11-04T01:00:00Z")


def book(t,bid,ask=None):
    return Book(t,((bid,10.),),((bid+.1 if ask is None else ask,10.),))


def test_no_quote_age_cutoff_1s_mode_vs_old_20s():
    # No genuine source gap in real 1s source; fixture verifies caller explicitly
    # chooses no filtering instead of silently accepting legacy 20-second cutoff.
    x=[book(T,5000.)]
    p=[book(T+45_000,5003.),book(T+50_000,4999.)]
    z={"short":[("P75",-2.)],"long":[]}
    old,_,_=run_scenario(x,p,[],z,.5,2.,max_quote_age_ms=20_000)
    now,_,_=run_scenario(x,p,[],z,.5,2.,max_quote_age_ms=None)
    assert old["paxg_maker_orders_posted"]==0
    assert now["paxg_maker_orders_posted"]==1
    assert now["max_cross_market_quote_age_s"] is None


def test_price_through_and_no_lookahead_on_streaming_timeline():
    z={"short":[("P75",-2.)],"long":[]}
    # First arm outside then enter -> post pending; next PAXG SELL trade
    # through maker bid; XAU hedge must use strictly subsequent snapshot.
    xs=[book(T,5000.),book(T+2_000,5000.),book(T+4_000,5000.)]
    ps=[book(T+1_000,5003.),book(T+2_000,4999.),
        book(T+5_000,5001.2)]
    tape=[Tape(T+3_000,4998.5,.001,"A","through")]
    seq=iter(sorted([(r.ts,0,r) for r in xs]+[(r.ts,1,r) for r in ps]+
                    [(r.ts,2,r) for r in tape], key=lambda e:(e[0],e[1])))
    result,closed,terminal=run_scenario([],[],[],z,.5,2.,
        max_quote_age_ms=None,timeline_override=seq)
    assert result["maker_orders_with_any_public_trade_allocated"]==1
    assert result["maker_assumed_fill_quantity_oz"]==.5
    assert result["xau_hedges_executed"]==1
    assert closed and not terminal
    assert closed[0]["hedge_utc"].startswith("2026-11-04T01:00:04")


def test_fee_repricing_consistent_without_modifying_fill_decisions():
    r={"qty_oz":.5,"paxg_maker_px":4000.,
       "xau_hedge_px":4001.,"xau_exit_px":3998.,
       "paxg_exit_px":3999.,"gross_usd":2.}
    entry,exit=fees_for_trade(r,.5,.5)
    assert entry==pytest.approx(.5*(4000+4001)*.5/10000)
    assert exit==pytest.approx(.5*(3998+3999)*.5/10000)
    baseline={"terminal_unmarked_positions":0,"max_complete_mark_drawdown_usd":0.}
    a,closed,terminal=derive_fee_profiles(baseline,[r],[],.5,.5)
    assert a["closed_net_usd"]==round(2.-entry-exit,6)
    assert not terminal
    assert closed[0]["gross_usd"]==2.


def test_fee_reprice_terminal_unhedged_only_paxg_two_fees():
    b={"terminal_unmarked_positions":0,"max_complete_mark_drawdown_usd":0.}
    r={"status":"hedge_wait","qty_oz":1.,"paxg_maker_px":4000.,
       "xau_hedge_px":None,"xau_exit_px":None,
       "paxg_exit_px":3998.,"gross_usd":2.}
    s,_,terminal=derive_fee_profiles(b,[],[r],1.,2.)
    assert s["total_net_with_terminal_liquidation_usd"]==round(
        2. - 4000/10000- 3998*2/10000,6)
    assert terminal[0]["status"]=="hedge_wait"


def test_unmarked_terminal_fails_closed_not_fake_zero():
    b={"terminal_unmarked_positions":1,"max_complete_mark_drawdown_usd":0.}
    s,_,_=derive_fee_profiles(b,[],[],0.,0.)
    assert s["total_net_with_terminal_liquidation_usd"] is None


def test_stream_books_and_merge_utc_order(tmp_path):
    root=tmp_path
    rows=[]
    for sym,base in [("XAU",5000.),("PAXG",4999.)]:
        folder=root/"l2_1s"/sym
        folder.mkdir(parents=True)
        f=folder/"part.jsonl.gz"
        with gzip.open(f,"wt") as out:
            for sec in (1,2,3):
                row={"timestamp":ms(T+sec*1000),"bids":[[str(base),"5"]],
                     "asks":[[str(base+0.1),"5"]]}
                out.write(json.dumps(row)+"\n")
        rows.append({"symbol":sym,"start":"2026-11-04T01:00:00Z",
                     "end":"2026-11-04T01:15:00Z","file":f.name,"row_count":"3"})
    record={sym:[r for r in rows if r["symbol"]==sym] for sym in ("XAU","PAXG")}
    counts=Counter()
    tape=[Tape(T+2000,4999.,.01,"A","trade")]
    order=list(merge_timeline(root,record,tape,counts))
    assert [(e[0]-T,e[1]) for e in order]==[
        (1000,0),(1000,1),(2000,0),(2000,1),(2000,2),(3000,0),(3000,1)]
    assert counts["XAU_parsed"]==3
    assert counts["PAXG_parsed"]==3
