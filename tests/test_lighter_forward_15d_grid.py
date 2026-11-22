from scripts.analyze_lighter_spread_feasibility import Book
from scripts.forwardtest_lighter_spread_grid_15d import (
    Position, TEST_START, TEST_END, FEE_PROFILES, execution,
    gross_oz, vwap_slice, price_is_inside, price_is_outside, run_scenario,
)
import pytest


def event(offset_minutes, spread, depth=100):
    ts=TEST_START+offset_minutes*60000
    xbid=5000+spread
    p_bid=5000
    x=Book(ts, ((xbid, depth),), ((xbid+0.1,depth),))
    p=Book(ts, ((p_bid,depth),), ((p_bid+0.1,depth),))
    return ts,x,p


ZONES={
    "short":[("P75",-2.),("P90",0.),("P95",1.)],
    "long":[("P25",-6.),("P10",-8.),("P5",-10.)],
}


def test_order_book_slice_consumes_shared_depth():
    b=Book(TEST_START,((10.,.5),(9.,1.)),((11.,.5),(12.,1.)))
    assert vwap_slice(b,"sell",1.,0.)==9.5
    assert vwap_slice(b,"sell",1.,.5)==9.
    assert vwap_slice(b,"buy",1.,.5)==12.
    assert vwap_slice(b,"buy",2.,0.) is None


def test_two_sided_grid_one_active_until_tp_then_recross():
    # First -4 is outside SHORT and LONG trigger zones, arms all six.
    # Each up-cross opens SHORT. Each down-cross of -11 opens LONG.
    levels=[-4.,2.,2.,-4.,2.,-4.,-11.,-4.,-11.,-4.]
    events=[event(i,s) for i,s in enumerate(levels)]
    r,trades,mark=run_scenario(
        events,ZONES,1.,2.,"maker_assumed","standard",0.,0.)
    assert r["tp_closed_trades"]==12
    assert r["tp_short_count"]==6
    assert r["tp_long_count"]==6
    assert r["unclosed_grids"]==0
    assert r["max_concurrent_grids"]==3
    assert len(trades)==12
    assert r["realized_net_usd"]>0
    assert mark==[]


def test_tp_2_closes_but_tp_3_remains_open():
    # SHORT opens at -1.5, improves to -3.9: enough for 2 but not 3.
    events=[event(0,-4.),event(1,-1.5),event(2,-3.9)]
    zones={"short":[("P75",-2.)],"long":[("P25",-10.)]}
    two,trades_two,_=run_scenario(events,zones,1.,2.,"maker_assumed","standard",0.,0.)
    three,trades_three,mark_three=run_scenario(events,zones,1.,3.,"maker_assumed","standard",0.,0.)
    assert two["tp_closed_trades"]==1
    assert three["tp_closed_trades"]==0
    assert three["unclosed_grids"]==1
    assert len(mark_three)==1
    assert three["total_net_including_terminal_mark_usd"] is not None


def test_fee_applies_to_both_instruments_all_four_executions():
    events=[event(0,-4.),event(1,2.),event(2,-4.)]
    zones={"short":[("P90",0.)],"long":[("P5",-10.)]}
    nofee,t0,_=run_scenario(events,zones,1.,2.,"maker_assumed","standard",0.,0.)
    plus,t1,_=run_scenario(events,zones,1.,2.,"maker_assumed","plus",.5,.5)
    assert len(t0)==len(t1)==1
    assert t1[0]["entry_fee_usd"]>0
    assert t1[0]["exit_fee_usd"]>0
    assert plus["realized_net_usd"]<nofee["realized_net_usd"]
    # Maker-only entry PAXG vs taker benchmark must pay worse price on entry.
    taker,tt,_=run_scenario(events,zones,1.,2.,"taker","standard",0.,0.)
    assert taker["realized_net_usd"]<nofee["realized_net_usd"]


def test_quote_crossing_and_no_fabricated_trades():
    assert price_is_outside("short",-3.,-2.)
    assert not price_is_inside("short",-3.,-2.)
    assert price_is_inside("long",-9.,-8.)
    assert not price_is_outside("long",-9.,-8.)
    noentry=[event(0,2.),event(1,1.),event(2,1.5)]
    r,trades,marks=run_scenario(noentry,ZONES,1.,2.,"maker_assumed","standard",0.,0.)
    # Starts already in short zone; must first observe OUTSIDE to arm.
    assert r["tp_closed_trades"]==0
    assert r["opens"]==0


def test_open_not_counted_as_realized_profit():
    events=[event(0,-4.),event(1,2.),event(2,3.)]
    zones={"short":[("P90",0.)],"long":[("P5",-10.)]}
    r,trades,marks=run_scenario(events,zones,1.,3.,"maker_assumed","standard",0.,0.)
    assert len(trades)==0
    assert len(marks)==1
    assert r["realized_net_usd"]==0
    assert r["total_net_including_terminal_mark_usd"]<0
