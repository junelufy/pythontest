"""Offline deterministic checks: mode center, 2-second hedge and grid semantics."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from scripts.backtest_lighter_mode_grid_1s import (
    TRAIN_END, HEDGE_LATENCY_MS, modal_histogram_center,
    zones_for_observed, qualifies, leave_zone, fee_reprice, replay,
    spread_refs, pnl_per_oz,
)


@dataclass
class Book:
    ts: int
    bid: tuple
    ask: tuple


def book(t, bid, ask=None, depth=50):
    return Book(t, ((float(bid), float(depth)),),
                ((float(bid + .1 if ask is None else ask), float(depth)),))


def trade(t, price, side, quantity=.01):
    return (t, 2, SimpleNamespace(ts=t, price=price, taker_side=side, qty=quantity))


def short_case(close=True, through=True, cancel=True):
    t = TRAIN_END
    ev = [(t + i * 1000, 0, book(t + i * 1000, 100.)) for i in range(0, 7)]
    def p(at, px):
        ev.append((t + at, 1, book(t + at, px)))
    p(1000, 105.5)       # short reference=-5.5, outside grid=-4
    p(2000, 104.0)       # -4 => POST BUY 104
    p(3000, 103.0)       # -3; same order must remain (no reprice)
    if cancel:
        p(4000, 104.5)   # -4.5 => CANCEL
        p(5000, 103.8)   # -3.8 => RE-POST BUY 103.8
        when = 6000
        limit = 103.8
    else:
        when = 4000
        limit = 104.
    if through:
        ev.append(trade(t + when, limit - .3, 'A'))  # sweeps through resting BUY
    ev.append((t + when + 1000, 0, book(t + when + 1000, 100.)))
    ev.append((t + when + 1999, 0, book(t + when + 1999, 100.)))
    ev.append((t + when + 2000, 0, book(t + when + 2000, 100.)))
    if close:
        p(when + 3000, limit + 2.3)  # held gross >2 at bid/ask
    return sorted(ev, key=lambda x:(x[0], x[1]))


def test_mode_center_uses_most_common_range_not_mean_or_midrange():
    # Distorted tails, modal [ -6,-5 ) => center -5.5.
    sample = [-5.8]*80 + [-8.]*4 + [3.]*8 + [-15.]*2 + [-4.]*6
    center, winner, peak = modal_histogram_center(sample, 1.0)
    assert (center, winner, peak) == (-5.5, -6, 80)
    assert sum(sample)/len(sample) != center


def test_dynamic_grid_is_center_plus_minus_fixed_steps():
    z={}
    zones_for_observed(-5., 1., {'short': -2.6, 'long': -8.2}, z, 64)
    assert [(z[f'short_{i}'].level) for i in (1,2)] == [-4.,-3.]
    assert [z[f'long_{i}'].level for i in (1,2,3)] == [-6.,-7.,-8.]
    assert z['long_3'].status == 'flat'
    with pytest.raises(RuntimeError):
        zones_for_observed(-5., 1., {'short': 300.,'long':None}, z, 64)


def test_threshold_equal_is_pending_not_cancelled():
    assert qualifies('short', -4., -4.)
    assert not leave_zone('short', -4., -4.)
    assert leave_zone('short', -4.01, -4.)
    assert qualifies('long', -6., -6.)
    assert not leave_zone('long', -6., -6.)
    assert leave_zone('long', -5.99, -6.)


def test_short_cancel_repost_then_through_fill_and_two_second_hedge():
    t=TRAIN_END
    result, closed, terminal, audit, grids = replay(
        short_case(), -5., 1., .5, 2.,
        test_start=t, test_end=t+20000, record_events=True)
    relevant=[e for e in audit if e['grid']=='short_1']
    types=[e['event'] for e in relevant]
    assert types.count('POST') == 2
    assert types.count('CANCEL_SPREAD') == 1
    assert 'MAKER_PAPER_FILL' in types
    assert 'XAU_HEDGE' in types
    hedge=next(e for e in relevant if e['event']=='XAU_HEDGE')
    fill=next(e for e in relevant if e['event']=='MAKER_PAPER_FILL')
    assert hedge['delay_ms'] == HEDGE_LATENCY_MS == 2000
    assert hedge['time_utc'] > fill['time_utc']
    assert result['xau_hedges'] == 1
    assert result['tp_closed'] == 1
    assert len(closed)==1
    assert closed[0]['paxg_entry_px']==103.8
    assert closed[0]['gross_per_oz_usd'] >= 2.
    assert grids['short_1'].status == 'flat'


def test_no_public_trade_means_no_fill_even_if_spread_rises():
    t=TRAIN_END
    result, closed, terminal, audit, grids=replay(
        short_case(through=False,close=False,cancel=False), -5., 1., .5, 2.,
        test_start=t, test_end=t+20000, record_events=True)
    assert result['maker_posts'] >= 1
    assert result['maker_paper_fills'] == 0
    assert result['xau_hedges'] == 0
    assert not closed


def test_hedge_does_not_use_xau_books_before_two_seconds():
    t=TRAIN_END
    e=short_case(close=False,through=True,cancel=True)
    e=[row for row in e if row[0] <= t+7999]
    result,closed,terminal,audit,grids=replay(
        e,-5.,1.,.5,2.,test_start=t,test_end=t+20000,record_events=True)
    assert result['maker_paper_fills'] == 1
    assert result['xau_hedges'] == 0
    assert result['terminal_unhedged_paxg'] == 1


def test_spread_refs_use_taker_vwap_and_maker_limit():
    x=Book(TRAIN_END,((100.,1.),(99.,10.)),((101.,1.),(102.,10.)))
    p=book(TRAIN_END,104.)
    s=spread_refs(x,p,1.5)
    assert s['short'] < -4.
    assert s['long'] > -4.


def test_long_maker_sell_and_buy_xau_hedge_two_seconds():
    t=TRAIN_END
    ev=[(t,0,book(t,100.)),
        (t+1000,1,book(t+1000,104.9)), # long ref XAU buy 100.1 - ask105 = -4.9 outside -6
        (t+2000,1,book(t+2000,106.0)), # long ref=-6.0 -> post SELL PAXG ask106.1
        trade(t+3000,106.4,'B'),         # public taker BUY sweeps SELL limit
        (t+4000,0,book(t+4000,100.)),
        (t+5000,0,book(t+5000,100.))]
    r,c,terminal,audit,grids=replay(sorted(ev,key=lambda x:(x[0],x[1])),
        -5.,1.,.5,2.,test_start=t,test_end=t+15000,record_events=True)
    assert any(e['event']=='POST' and e['grid']=='long_1' and e['maker_action']=='SELL' for e in audit)
    assert r['maker_paper_fills']==1
    assert r['xau_hedges']==1
    assert r['max_observed_hedge_delay_ms']==2000
    assert r['terminal_open_hedged']==1


def test_fee_repricing_correct_for_open_hedged_and_unhedged():
    a={'qty_oz':.5,'paxg_entry_px':4000.,'xau_entry_px':4001.,
       'paxg_exit_px':3999.,'xau_exit_px':3998.,'gross_usd':2.}
    net,fees=fee_reprice(a,.5,2.8)
    expected=.5*4000*.5/10000 + .5*(4001+3998+3999)*2.8/10000
    assert fees==pytest.approx(round(expected,6))
    assert net==pytest.approx(round(2-expected,6))
    a['xau_entry_px']=a['xau_exit_px']=None
    _,only_paxg=fee_reprice(a,.5,2.8)
    assert only_paxg==pytest.approx(round(.5*(4000*.5+3999*2.8)/10000,6))


def test_no_false_same_timestamp_maker_fill():
    t=TRAIN_END
    e=[(t,0,book(t,100.)),(t+1000,1,book(t+1000,105.5)),
       (t+2000,1,book(t+2000,104.)),trade(t+2000,103.5,'A')]
    r,_,_,_,_=replay(e,-5.,1.,.5,2.,test_start=t,test_end=t+10000)
    assert r['maker_posts']==1 and r['maker_paper_fills']==0


def test_seven_day_histogram_calibration_excludes_test_chunks(monkeypatch):
    """Physically excludes records from all >=7d chunks, no future leakage."""
    from scripts import backtest_lighter_mode_grid_1s as m
    import math
    t=m.TRAIN_BEGIN
    seen=[]
    fake_records={sym:[{"start":m.FIRST, "file":"first7days"},
                      {"start":m.iso(m.TRAIN_END),"file":"TEST_DATA_MUST_NOT_OPEN"}]
                  for sym in ("XAU","PAXG")}
    def fake_iter(source, sym, records, counters):
        assert len(records)==1
        assert records[0]['file']=='first7days'
        seen.append(sym)
        for i in range(7*1440):
            now=t+i*60000
            # 90% of training minutes in [-6,-5), 10% in [-10,-9).
            paxg=105.5 if i%10 else 109.5
            counters[sym+'_rows_seen']+=1
            if sym=='XAU':
                yield book(now,100.,100.1)
            else:
                yield book(now,paxg,paxg+.1)
    monkeypatch.setattr(m,'iter_books',fake_iter)
    report,rows=m.calibration_distribution('unused',fake_records,1.0,3000)
    assert seen==['XAU','PAXG'] or sorted(seen)==['PAXG','XAU']
    assert report['n_sampled_minutes']==7*1440
    assert report['center_usd_oz']==-5.5
    assert report['modal_bin_minutes']==7*1440-(7*1440)//10
    assert sum(x['minutes'] for x in rows)==7*1440


def test_exact_price_public_taker_print_does_not_skip_fifo_queue():
    t=TRAIN_END
    ev=[(t,0,book(t,100.)),(t+1000,1,book(t+1000,105.5)),
        (t+2000,0,book(t+2000,100.)),(t+2000,1,book(t+2000,104.)),
        trade(t+3000,104.,'A',quantity=.1)]
    r,closed,terminal,audit,grids=replay(ev,-5.,1.,.5,2.,test_start=t,test_end=t+10000)
    assert r['maker_posts']==1
    assert r['eligible_public_prints']==1
    assert r['maker_paper_fills']==0  # 50oz visible queue ahead > 0.1oz print
    assert not closed


def test_end_to_end_report_from_synthetic_source(tmp_path, monkeypatch):
    """Smoke main driver + 12 scenario CSV/JSON files without external APIs."""
    import json
    import csv
    from scripts import backtest_lighter_mode_grid_1s as m
    history=short_case()
    def fake_manifest(source):
        return {'XAU':[{'start':m.FIRST}],'PAXG':[{'start':m.FIRST}]}, {
            'snapshots_per_symbol':{'XAU':2,'PAXG':2},'source_complete':True}
    def fake_calibrate(*args):
        return {'center_usd_oz':-5.,'sample_method':'fake','n_sampled_minutes':10080}, [
            {'lower_usd_oz':-6,'upper_usd_oz':-5,'minutes':9000,'fraction':.9,'modal_bin':True}]
    def fake_tape(source):
        return [row[2] for row in history if row[1] == 2], {'taker_trades':1}
    def fake_merge(source,records,tape,counters):
        # Synthetic event tape is shared but the 1s raw hash/coverage check must
        # be satisfied by the stub. The actual source uses full manifest rows.
        counters['XAU_rows_seen']=2
        counters['PAXG_rows_seen']=2
        counters['XAU_parsed']=2
        counters['PAXG_parsed']=2
        return iter(history)
    monkeypatch.setattr(m,'get_manifest',fake_manifest)
    monkeypatch.setattr(m,'calibration_distribution',fake_calibrate)
    monkeypatch.setattr(m,'load_taker_tape',fake_tape)
    monkeypatch.setattr(m,'merge_timeline',fake_merge)
    out=tmp_path/'out'
    report=m.run('fake','fake',out)
    assert len(report['scenarios'])==12
    assert report['fixed_hedge_latency_ms']==2000
    assert (out/'summary.json').is_file()
    assert json.loads((out/'summary.json').read_text())['calibration']['center_usd_oz']==-5.
    with (out/'scenarios.csv').open() as f:
        assert len(list(csv.DictReader(f)))==12
    assert (out/'grid_levels.csv').stat().st_size>0
