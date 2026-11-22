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
    assert abs(sum(t["qty_oz"] for t in closed) - .1) < 1e-9
    # Final XAU bid=5000 and PAXG bid=5001.2 -> SHORT ref=-1.2.
    # This is below levels P90=-1 and P95=0: both remaining makers
    # MUST cancel under the user's spread-based pending cancellation rule.
    assert r["terminal_pending_unfilled"]==0
    assert r["maker_orders_canceled_on_spread_reversion"]==2


def test_shared_queue_other_grids_remain_pending_if_short_spread_stays_in_zone():
    # Same three maker orders and exactly one shared 0.6 oz trade:
    # 0.5 oz queue ahead + 0.1 oz only for first grid. The final
    # XAU 5000 - PAXG 4998 = +2, so the two other makers MUST remain
    # pending; their unfilled quantities cannot be invented as fills.
    x,p,_=fixture(final_paxg_bid=4998.)
    zones={"short":[("P75",-2.),("P90",-1.),("P95",0.)],"long":[]}
    tape=[Tape(TEST_START+62000,4999.0,.6,"A","one-taker-print")]
    r,closed,terminal=run_scenario(x,p,tape,zones,.5,2.)
    assert r["paxg_maker_orders_posted"]==3
    assert r["maker_orders_with_any_public_trade_allocated"]==1
    assert abs(r["maker_matched_public_volume_oz"]-.1)<1e-9
    assert r["maker_orders_canceled_on_spread_reversion"]==0
    assert r["terminal_pending_unfilled"]==2
    assert r["terminal_open_hedged_grids"]==1
    assert closed==[]
    assert len(terminal)==1


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



def test_price_through_buy_maker_fills_and_waits_for_next_xau_hedge():
    x,p,_=fixture(final_paxg_bid=4998.0)
    events=[]
    # An aggressor SELL trade at 4998.8 passed the standing BUY maker
    # limit 4999.0. Conditional on a live, accepted maker order and
    # normal price priority, all 0.5 oz must have been executed at 4999.
    tape=[Tape(TEST_START+62000,4998.8,.01,"A","through-one-print")]
    result,closed,terminal=run_scenario(
        x,p,tape,ZONES,.5,2.,event_log=events
    )
    assert result["paxg_maker_orders_posted"]==1
    assert result["potential_public_prints_at_or_through_pending_limit"]==1
    assert result["potential_price_through_public_prints"]==1
    assert result["potential_exact_price_public_prints"]==0
    assert result["maker_orders_with_any_public_trade_allocated"]==1
    assert result["maker_through_price_order_full_fills_assumed"]==1
    assert result["maker_matched_public_volume_oz"]==.5
    assert result["xau_hedges_executed"]==1
    assert result["tp_closed_positions"]==0
    assert not closed and len(terminal)==1
    assert terminal[0]["qty_oz"]==.5
    assert [e["type"] for e in events].count("THROUGH_TRADE_PAPER_FULL_FILL")==1


def test_price_through_sell_maker_fills_on_aggressive_buy_above_limit():
    t=TEST_START
    x=[book(t,5000),book(t+60000,5000),book(t+63000,5000)]
    p=[book(t+1000,5003),
       book(t+61000,5008.9,ask=5009.,size=.5),
       book(t+64000,5008.9,ask=5009.)]
    longonly={"short":[],"long":[("P10",-8.)]}
    tape=[Tape(t+62000,5009.5,.01,"B","aggressor-buy-through-ask")]
    r,closed,terminal=run_scenario(x,p,tape,longonly,.5,3.)
    assert r["paxg_maker_orders_posted"]==1
    assert r["maker_through_price_order_full_fills_assumed"]==1
    assert r["xau_hedges_executed"]==1
    assert r["terminal_open_hedged_grids"]==1
    assert r["maker_matched_public_volume_oz"]==.5
    assert closed==[]
    assert len(terminal)==1


def test_three_grid_price_through_does_not_double_count_public_trade_volume():
    x,p,_=fixture(final_paxg_bid=4998.)
    zones={"short":[("P75",-2.),("P90",-1.),("P95",0.)],"long":[]}
    tape=[Tape(TEST_START+62000,4998.8,.01,"A","sweep-through")]
    r,closed,terminal=run_scenario(x,p,tape,zones,.5,2.)
    # One 0.01 oz *worse-price print* implies earlier better resting
    # makers 1.5 oz had been executed IF the hypothetical orders were live.
    # It is not treating 0.01 as available size for three distinct orders.
    assert r["paxg_maker_orders_posted"]==3
    assert r["maker_through_price_order_full_fills_assumed"]==3
    assert r["maker_matched_public_volume_oz"]==1.5
    assert r["xau_hedges_executed"]==3
    assert r["tp_closed_positions"]==0
    assert closed==[]
    assert len(terminal)==3


def test_through_trade_after_pending_spread_cancellation_cannot_fill():
    t=TEST_START
    x=[book(t,5000),book(t+60000,5000),book(t+120000,5000)]
    p=[book(t+1000,5003),book(t+61000,4999),book(t+121000,5003)]
    # Post at 61s and cancel on spread at 121s, then trade through at 122s.
    trade=[Tape(t+122000,4998.0,5.,"A","too-late-sweep")]
    r,closed,terminal=run_scenario(x,p,trade,ZONES,.5,2.)
    assert r["paxg_maker_orders_posted"]==1
    assert r["maker_orders_canceled_on_spread_reversion"]==1
    assert r["maker_through_price_order_full_fills_assumed"]==0
    assert r["xau_hedges_executed"]==0
    assert closed==[] and terminal==[]


def test_exact_price_print_diagnostic_matches_existing_queue_simulator():
    x,p,trades=fixture()
    events=[]
    result,closed,_=run_scenario(
        x,p,trades,ZONES,.5,2.,event_log=events
    )
    assert result["potential_public_prints_at_or_through_pending_limit"]==1
    assert result["potential_exact_price_public_prints"]==1
    assert result["potential_price_through_public_prints"]==0
    assert result["maker_orders_with_any_public_trade_allocated"]==1
    assert [e["type"] for e in events].count("EXACT_TRADE_PAPER_FILL")==1


def test_pending_cancel_audit_log_exposes_zone_trigger_and_elapsed():
    t=TEST_START
    x=[book(t,5000),book(t+60000,5000),book(t+120000,5000)]
    p=[book(t+1000,5003),book(t+61000,4999),book(t+121000,5003)]
    events=[]
    result,closed,terminal=run_scenario(
        x,p,[],ZONES,.5,2.,event_log=events
    )
    cancels=[e for e in events if e["type"]=="CANCEL_SPREAD"]
    assert len(cancels)==1
    assert cancels[0]["ref_spread_usd_oz"] < -2.0
    assert cancels[0]["maker_pending_seconds"] > 0
    assert result["terminal_pending_unfilled"]==0
    assert not closed and not terminal



def test_diagnostic_csv_preserves_fields_from_different_event_types(tmp_path):
    import csv
    from scripts.replay_lighter_causal_maker_first_15d import write_csv
    target=tmp_path/"events.csv"
    write_csv(target,[
        {"time_utc":"2026-11-10T01:00:00Z","type":"POST","maker_limit":100},
        {"time_utc":"2026-11-10T01:01:00Z","type":"PUBLIC_PRINT_AT_OR_THROUGH","public_price":99},
        {"time_utc":"2026-11-10T01:02:00Z","type":"CANCEL_SPREAD","maker_pending_seconds":120},
    ],["type"])
    with target.open(newline="",encoding="utf8") as f:
        rows=list(csv.DictReader(f))
    assert len(rows)==3
    assert rows[0]["maker_limit"]=="100"
    assert rows[1]["public_price"]=="99"
    assert rows[2]["maker_pending_seconds"]=="120"



def test_spread_move_from_xau_only_does_not_imply_paxg_maker_fill():
    t=TEST_START
    # XAU changes the SPREAD while PAXG market has zero public trades.
    # A touched spread grid IS NOT equivalent to a PAXG maker execution.
    x=[book(t,5000.),book(t+60000,5000.),
       book(t+120000,5002.),book(t+180000,5004.)]
    p=[book(t+1000,5003.),book(t+61000,4999.),
       book(t+121000,4999.),book(t+181000,4999.)]
    r,closed,terminal=run_scenario(x,p,[],ZONES,.5,2.)
    assert r["paxg_maker_orders_posted"]==1
    assert r["maker_through_price_order_full_fills_assumed"]==0
    assert r["maker_orders_with_any_public_trade_allocated"]==0
    assert r["xau_hedges_executed"]==0
    assert r["tp_closed_positions"]==0
    assert closed==[] and terminal==[]


def test_exact_maker_print_smaller_than_visible_queue_is_not_a_fill():
    x,p,_=fixture(final_paxg_bid=4998.5)
    tape=[Tape(TEST_START+62000,4999.,.20,"A","small-exact-print")]
    r,closed,terminal=run_scenario(x,p,tape,ZONES,.5,2.)
    assert r["maker_limit_exact_trade_prints"]==1
    assert r["maker_orders_with_any_public_trade_allocated"]==0
    assert r["maker_through_price_order_full_fills_assumed"]==0
    assert r["xau_hedges_executed"]==0
    assert r["terminal_pending_unfilled"]==1
    assert closed==[] and terminal==[]


def test_wrong_aggressor_price_through_does_not_execute_passive_buy():
    x,p,_=fixture(final_paxg_bid=4998.5)
    # Passive BUY at 4999 needs aggressor SELL (A). An aggressor BUY (B)
    # at 4998.5 is not evidence that a resting BUY at 4999 filled.
    tape=[Tape(TEST_START+62000,4998.5,1.,"B","wrong-side-through")]
    r,closed,terminal=run_scenario(x,p,tape,ZONES,.5,2.)
    assert r["potential_public_prints_at_or_through_pending_limit"]==0
    assert r["maker_through_price_order_full_fills_assumed"]==0
    assert r["xau_hedges_executed"]==0
    assert r["terminal_pending_unfilled"]==1
    assert closed==[] and terminal==[]
