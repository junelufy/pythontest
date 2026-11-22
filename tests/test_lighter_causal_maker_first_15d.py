import gzip
import json

from scripts.analyze_lighter_spread_feasibility import Book, iso, ms
from scripts.replay_lighter_causal_maker_first_15d import (
    TEST_START, TEST_END, Tape, load_taker_tape, run_scenario
)


def book(t, bid, ask=None, size=20.):
    if ask is None:ask=bid+.10
    return Book(t,((bid,size),),((ask,size),))


ZONES={"short":[("P75",-2.)], "long":[]}


def fixture(trades=None, final_paxg_bid=5001.2, queue=.5):
    t=TEST_START
    x=[
        book(t,5000),
        book(t+60000,5000),
        book(t+63000,5000),
    ]
    p=[
        book(t+1000,5003),
        book(t+61000,4999,size=queue),
        book(t+64000,final_paxg_bid),
    ]
    if trades is None:
        trades=[Tape(t+62000,4999.0,.6,"A","maker-touched")]
    return x,p,trades


def test_partial_maker_fill_hedged_only_after_future_xau_book():
    x,p,trades=fixture()
    result,closed,terminal=run_scenario(x,p,trades,ZONES,.5,2.)
    assert result["paxg_maker_orders_posted"]==1
    assert result["maker_orders_with_any_public_trade_allocated"]==1
    assert result["maker_matched_public_volume_oz"]==.1
    assert result["xau_hedges_executed"]==1
    assert result["tp_closed_positions"]==1
    assert not terminal
    assert abs(closed[0]["qty_oz"]-.1)<1e-7
    assert closed[0]["hedge_utc"]==iso(TEST_START+63000)
    assert closed[0]["entry_utc"]==iso(TEST_START+62000)
    assert closed[0]["exit_utc"]==iso(TEST_START+64000)


def test_tp_2_vs_tp_3_frozen_threshold():
    x,p,trades=fixture()
    two,closed2,_=run_scenario(x,p,trades,ZONES,.5,2.)
    three,closed3,terminal3=run_scenario(x,p,trades,ZONES,.5,3.)
    assert two["tp_closed_positions"]==1
    assert three["tp_closed_positions"]==0
    assert three["terminal_open_hedged_grids"]==1
    assert len(terminal3)==1


def test_wrong_taker_side_and_older_print_never_fill():
    x,p,_=fixture()
    bad=[Tape(TEST_START+61500,4999.0,10.,"B","wrong-aggressor")]
    r,closed,terminal=run_scenario(x,p,bad,ZONES,.5,2.)
    assert r["maker_orders_with_any_public_trade_allocated"]==0
    assert r["xau_hedges_executed"]==0
    assert not closed and not terminal
    # The same price and side printed at placement timestamp is not future evidence.
    bad2=[Tape(TEST_START+61000,4999.0,10.,"A","same-timestamp")]
    r2,closed2,_=run_scenario(x,p,bad2,ZONES,.5,2.)
    assert r2["maker_orders_with_any_public_trade_allocated"]==0
    assert not closed2


def test_three_grid_orders_share_public_print_volume_and_queue_ahead():
    x,p,_=fixture()
    zones={"short":[("P75",-2.),("P90",-1.),("P95",0.)],"long":[]}
    print1=Tape(TEST_START+62000,4999.0,.6,"A","one-taker-print")
    r,closed,terminal=run_scenario(x,p,[print1],zones,.5,2.)
    assert r["paxg_maker_orders_posted"]==3
    # 0.5 oz quote-ahead consumed; 0.1 oz allocated to exactly one order.
    assert r["maker_orders_with_any_public_trade_allocated"]==1
    assert r["maker_matched_public_volume_oz"]==.1
    assert sum(t["qty_oz"] for t in closed)==.1
    assert r["terminal_pending_unfilled"]==2


def test_quote_age_filter_prevents_46_second_stale_xau_entries():
    x,p,_=fixture()
    old_x=[book(TEST_START,5000)]
    r,closed,_=run_scenario(old_x,p,[],ZONES,.5,2.)
    assert r["paxg_maker_orders_posted"]==0
    assert r["paxg_snapshot_no_fresh_xau"]>=2
    assert not closed


def test_unhedged_partial_paxg_exposure_is_marked_separately():
    x,p,trades=fixture()
    # No XAU snapshot after the public partial fill at t+62s.
    past_x=x[:2]
    result,closed,terminal=run_scenario(past_x,p,trades,ZONES,.5,2.)
    assert not closed
    assert result["terminal_hedge_wait_unhedged_paxg"]==1
    assert result["terminal_open_hedged_grids"]==0
    assert terminal[0]["status"]=="hedge_wait"
    assert terminal[0]["qty_oz"]>0
    assert result["incomplete_equity_mark_samples"]>0


def test_raw_archive_double_sided_trade_deduplicated_on_taker_leg(tmp_path):
    folder=tmp_path/"trades"/"PAXG"
    folder.mkdir(parents=True)
    file=folder/f"{TEST_START}_{TEST_END}.jsonl.gz"
    when=iso(TEST_START+62000)
    maker={"timestamp":when,"trade_id":123,"side":"B",
           "price":"4999","size":"0.15","crossed":False}
    taker={"timestamp":when,"trade_id":123,"side":"A",
           "price":"4999","size":"0.15","crossed":True}
    with gzip.open(file,"wt",encoding="utf8") as f:
        for item in [maker,taker,taker]:
            f.write(json.dumps(item)+"\n")
    tape,diag=load_taker_tape(tmp_path)
    assert len(tape)==1
    assert tape[0].taker_side=="A"
    assert tape[0].qty==.15
    assert diag["taker_deduplicated"]==1


def test_no_premature_exit_no_historical_lookahead():
    x,p,trades=fixture(final_paxg_bid=4999.1)
    r,closed,terminal=run_scenario(x,p,trades,ZONES,.5,2.)
    assert not closed
    assert r["terminal_open_hedged_grids"]==1
    assert len(terminal)==1
    assert r["closed_net_usd"]==0



def test_short_pending_cancel_only_when_spread_falls_below_its_grid():
    t=TEST_START
    # XAU maker short-reference = XAU executable sell - PAXG best bid.
    # -3 arms grid -2; +1 crosses and posts; -2 equals threshold (keep);
    # -2.01 goes outside (cancel).
    x=[book(t,5000),book(t+60000,5000),book(t+120000,5000),
       book(t+180000,5000)]
    p=[book(t+1000,5003),book(t+61000,4999),
       book(t+121000,5002),book(t+181000,5002.01)]
    r,closed,terminal=run_scenario(x,p,[],ZONES,.5,2.)
    assert r["paxg_maker_orders_posted"]==1
    assert r["maker_orders_canceled_on_spread_reversion"]==1
    assert r["terminal_pending_unfilled"]==0
    assert r["xau_hedges_executed"]==0
    assert closed==[] and terminal==[]


def test_long_pending_cancel_only_when_spread_rises_above_its_grid():
    t=TEST_START
    # Long-reference = XAU executable buy - PAXG best ask.
    # -3 arms level -8; -9 enters; return to ~ -7 cancels unfilled.
    x=[book(t,5000),book(t+60000,5000),book(t+120000,5000)]
    p=[book(t+1000,5003),book(t+61000,5009),
       book(t+121000,5007)]
    longonly={"short":[],"long":[("P10",-8.)]}
    r,closed,terminal=run_scenario(x,p,[],longonly,.5,3.)
    assert r["paxg_maker_orders_posted"]==1
    assert r["maker_orders_canceled_on_spread_reversion"]==1
    assert r["terminal_pending_unfilled"]==0
    assert closed==[] and terminal==[]


def test_pending_maker_remains_after_more_than_five_minutes_if_spread_in_zone():
    t=TEST_START
    x=[book(t,5000),book(t+60000,5000),book(t+8*60000,5000)]
    p=[book(t+1000,5003),book(t+61000,4999),
       book(t+8*60000+1000,4999)]
    r,closed,terminal=run_scenario(x,p,[],ZONES,.5,2.)
    assert r["paxg_maker_orders_posted"]==1
    assert r["maker_orders_canceled_on_spread_reversion"]==0
    assert r["terminal_pending_unfilled"]==1
    assert r["tp_closed_positions"]==0
    assert closed==[] and terminal==[]
