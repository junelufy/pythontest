from dataclasses import dataclass
from types import SimpleNamespace
from pathlib import Path
import pytest

from scripts import backtest_lighter_grid_vnext_1s as v

@dataclass
class Book:
    ts:int
    bid:tuple
    ask:tuple

def book(t, bid, ask=None,depth=20):
    return Book(t,((float(bid),float(depth)),),
                ((float(bid+.1 if ask is None else ask),float(depth)),))

def x(t,bid,ask=None,depth=20):return (t,0,book(t,bid,ask,depth))
def p(t,bid,ask=None,depth=20):return (t,1,book(t,bid,ask,depth))
def trade(t,price,side,qty=.1):
    return (t,2,SimpleNamespace(ts=t,price=price,taker_side=side,qty=qty))
def ordered(items):return sorted(items,key=lambda e:(e[0],e[1]))

def scenario_no_exit(*,q=0.5,dwell=10_000,post_latency=1000,cancel_latency=1000):
    """XAU ask-bid on a short grid=3, baseline PAXG maker bid=100."""
    t=v.TRAIN_END
    events=[]
    for ms_since in range(1000,19_000,1000):
        now=t+ms_since
        events.extend([x(now,103.3,103.4),p(now,100,100.1,depth=.01)])
    cfg=v.Config(dwell_ms=dwell,post_latency_ms=post_latency,cancel_latency_ms=cancel_latency,
                 pending_cap=1,post_per_minute=100,cancel_per_minute=100)
    return t,events,cfg


def test_user_grid3_actual2_5_tp4_targets_minus1_5():
    assert v.entry_spread(102.5,100.0)==2.5
    assert v.tp_spread_target('short',2.5,4.)==-1.5
    assert v.exit_reached('short',-1.5,-1.5)
    assert not v.exit_reached('short',-1.,-1.5)
    assert v.tp_spread_target('long',-2.5,4.)==1.5
    assert v.cancel_required('short',1.99,3.,1.)
    assert not v.cancel_required('short',2.0,3.,1.)
    assert not v.cancel_required('short',2.5,3.,1.)
    assert v.cancel_required('long',-5.99,-7.,1.)
    assert not v.cancel_required('long',-6.,-7.,1.)


def test_grid3_slips_to_2_5_exit_anchored_actual_not_grid():
    t,e,cfg=scenario_no_exit()
    # Post at t+11000, ACK t+12000. Through trade from Taker SELL fills maker BUY.
    e += [trade(t+12500,99.5,'A'),
          x(t+14500,102.5,102.6),
          x(t+15500,98.9,99.0),p(t+15500,100.5,100.6)]
    result,closed,terminal,audit,grids=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                          test_start=t,test_end=t+20000,record_events=True)
    rows=[r for r in closed if r['grid']=='short_1']
    assert len(rows)==1, (result,closed,audit[-10:])
    row=rows[0]
    assert row['grid_level_usd_oz']==3.0
    assert row['actual_entry_spread_usd_oz']==pytest.approx(2.5)
    assert row['tp_distance_usd_oz']==4.
    assert row['target_exit_spread_usd_oz']==pytest.approx(-1.5)
    assert row['actual_exit_spread_usd_oz']==pytest.approx(-1.5)
    assert row['gross_per_oz_usd']==pytest.approx(4.)
    hedge=next(r for r in audit if r['event']=='XAU_HEDGE' and r['grid']=='short_1')
    assert hedge['hedge_delay_ms']==2000
    assert row['grid']=='short_1', 'slippage does NOT reassign grid to a different level'


def test_dwell_ten_seconds_is_real_contiguous_time_not_one_quote():
    t,e,cfg=scenario_no_exit()
    early=[r for r in e if r[0]<=t+10000]
    a,*_=v.paper_replay(ordered(early),2.,1.,.5,4.,cfg,test_start=t,test_end=t+20000)
    assert a['maker_posts']==0
    full,*_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,test_start=t,test_end=t+20000)
    assert full['maker_posts']==1
    # A data gap over 2s resets the dwell timer (do not count stale timestamps).
    gap=[r for r in e if r[0]<=t+6000 or r[0]>=t+15000]
    reset,*_=v.paper_replay(ordered(gap),2.,1.,.5,4.,cfg,test_start=t,test_end=t+20000)
    assert reset['maker_posts']==0


def test_buffer_holds_at_2_5_and_cancel_strict_below2():
    t,e,cfg=scenario_no_exit()
    e=[r for r in e if r[0]<=t+13000]
    e += [x(t+14000,102.5,102.6),p(t+14000,100,100.1),
          x(t+15000,101.9,102.0),p(t+15000,100,100.1)]
    result,_,_,audit,grids=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                        test_start=t,test_end=t+16000,record_events=True)
    assert any(r['event']=='CANCEL_SEND' and r['grid']=='short_1' for r in audit)
    when=next(r['time_utc'] for r in audit if r['event']=='CANCEL_SEND' and r['grid']=='short_1')
    assert when==v.iso(t+15000)
    assert result['maker_cancel_requests']==1


def test_posting_ack_and_cancel_ack_are_distinct_from_signal():
    t,e,cfg=scenario_no_exit(post_latency=1000,cancel_latency=2000)
    e=[r for r in e if r[0]<=t+13000]
    e += [x(t+14000,101.9,102.),p(t+14000,100,100.1),
          x(t+15000,101.8,101.9),p(t+15000,100,100.1),
          x(t+16000,101.8,101.9),p(t+16000,100,100.1)]
    r,_,_,audit,_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                test_start=t,test_end=t+17000,record_events=True)
    assert any(a['event']=='POST_SEND' for a in audit)
    assert any(a['event']=='POST_ACK' for a in audit)
    assert any(a['event']=='CANCEL_SEND' for a in audit)
    assert any(a['event']=='CANCEL_ACK' for a in audit)
    assert r['maker_post_acks']==1 and r['maker_cancel_acks']==1


def test_price_through_during_inflight_cancel_still_fills_and_hedges_once():
    t,e,cfg=scenario_no_exit(post_latency=1000,cancel_latency=2000)
    e=[r for r in e if r[0]<=t+13000]
    e += [x(t+14000,101.9,102.),p(t+14000,100,100.1),
          trade(t+14500,99.9,'A'),
          x(t+16500,102.5,102.6)]
    r,closed,terminal,audit,_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                               test_start=t,test_end=t+17000,record_events=True)
    assert r['fills_during_cancel_race']==1
    assert r['maker_fill_events']==1
    assert r['xau_hedges']==1
    assert any(a['event']=='MAKER_PAPER_FILL' for a in audit)


def test_spread_move_alone_is_not_public_taker_fill():
    t,e,cfg=scenario_no_exit()
    r,closed,_,audit,_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                      test_start=t,test_end=t+20000,record_events=True)
    assert r['maker_fill_events']==0 and not closed
    assert r['maker_posts']==1


def test_exact_print_consumes_queue_ahead_before_partial():
    t,e,cfg=scenario_no_exit()
    e=[r for r in e if r[0]<=t+13000]
    e += [trade(t+12500,100.,'A',.005)]
    r,*_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,test_start=t,test_end=t+20000)
    assert r['maker_fill_events']==0
    e += [trade(t+12800,100.,'A',.02)]
    r,*_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,test_start=t,test_end=t+20000)
    assert r['maker_fill_events']==1
    assert r['maker_cancel_requests']>=1, 'partial fills send Cancel of remainder'


def test_grid_level_four_can_open_while_grid_three_occupied():
    t,e,cfg=scenario_no_exit()
    cfg=v.Config(**{**vars(cfg),'pending_cap':2})
    e=[row for row in e if row[0]<=t+13000]
    e += [trade(t+12500,99.7,'A'),
          x(t+14500,104.2,104.3)]
    for offset in range(15,27):
        e.extend([x(t+offset*1000,104.2,104.3),p(t+offset*1000,100.,100.1)])
    result,closed,terminal,audit,grids=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                       test_start=t,test_end=t+30000,record_events=True)
    assert grids['short_1'].lots, 'Grid 3 stays consumed after its Fill'
    assert any(a['event']=='POST_SEND' and a['grid']=='short_2' for a in audit),audit[-10:]
    assert grids['short_2'].threshold==4.
    assert not any(a['event']=='POST_SEND' and a['grid']=='short_1' and a['time_utc']>v.iso(t+12500) for a in audit)


def test_pending_cap_prevents_more_than_two_simultaneous_live_makers():
    t,e,cfg=scenario_no_exit()
    cfg=v.Config(**{**vars(cfg),'pending_cap':2})
    e=[]
    for offset in range(1,17):
        e.extend([x(t+offset*1000,106.2,106.3),p(t+offset*1000,100.,100.1)])
    result,_,_,_,_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                  test_start=t,test_end=t+20000)
    assert result['maker_posts']==2
    assert result['max_active_makers']<=2


def test_post_only_cross_at_ack_rejects_without_paper_fill():
    t,e,cfg=scenario_no_exit(post_latency=2000)
    e=[r for r in e if r[0]<=t+11000]
    e += [p(t+12000,99.8,99.9),p(t+13000,99.8,99.9),trade(t+13500,99.0,'A')]
    r,_,_,audit,_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                 test_start=t,test_end=t+15000,record_events=True)
    assert r['post_only_rejections']>=1
    assert r['maker_fill_events']==0


def test_funding_one_leg_before_hedge_other_leg_after():
    start=v.ms('2026-11-11T01:00:00Z')
    end=v.ms('2026-11-11T04:00:00Z')
    row={'side':'short','qty_oz':1.,
         'maker_fill_utc':v.iso(start+1000),
         'hedge_utc':v.iso(start+2*3600000+1000),
         'exit_utc':v.iso(end)}
    idx={'PAXG':{start+3600000:2.,start+7200000:3.},
         'XAU':{start+3600000:4.,start+7200000:5.}}
    assert v.funding_for_position(row,idx,end)==-5.
    del idx['PAXG'][start+7200000]
    assert v.funding_for_position(row,idx,end) is None


def test_closed_trade_fee_reprice_uses_actually_filled_prices():
    row={'qty_oz':.5,'paxg_entry_px':100.,'xau_entry_px':102.5,
         'paxg_exit_px':100.5,'xau_exit_px':99.,'gross_usd':2.}
    result,closed,terminal=v.fee_and_funding({},[row],[],(.5,2.8),None)
    assert result['fee_only_net_with_terminal_usd']<2.
    assert result['net_after_fees_funding_terminal_usd'] is None


def test_config_rejects_invalid_cancel_buffer():
    with pytest.raises(ValueError):
        v.Config(cancel_buffer=-1).validate()


def test_filled_grid_3_does_not_recycle_on_pullback_while_grid4_is_next():
    # This specifically tests the user's 'grid 3 traded -> wait for grid 4' rule.
    t,e,cfg=scenario_no_exit()
    e=[r for r in e if r[0]<=t+13000]
    e += [trade(t+12500,99.5,'A'),x(t+14500,102.5,102.6),
          x(t+15500,102.2,102.3),p(t+15500,100.,100.1)]
    # TP=.2 at actual entry=2.5 -> exit=2.3, NEVER revisits center=2.
    # After TP close, short ref rises to 2.8 (below grid3=3 but above center2).
    for i in range(16,36):
        e += [x(t+i*1000,102.8,102.9),p(t+i*1000,100.,100.1)]
    # Then short ref rises again above 3. Do NOT repost used Grid 3 yet.
    for i in range(36,51):
        e += [x(t+i*1000,103.3,103.4),p(t+i*1000,100.,100.1)]
    _,closed,_,audit,grids=v.paper_replay(ordered(e),2.,1.,.5,.2,cfg,
                                          test_start=t,test_end=t+52000,record_events=True)
    assert len(closed)==1
    posts=[a for a in audit if a['event']=='POST_SEND' and a['grid']=='short_1']
    assert len(posts)==1
    assert grids['short_1'].rearm_after_close


def test_exposure_limit_reserves_pending_order_quantities():
    t,e,cfg=scenario_no_exit()
    cfg=v.Config(**{**vars(cfg),'pending_cap':10,'max_filled_exposure_oz':.5})
    e=[]
    for i in range(1,17):
        e.extend([x(t+i*1000,106.2,106.3),p(t+i*1000,100.,100.1)])
    r,*_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                         test_start=t,test_end=t+20000)
    assert r['maker_posts']==1
    assert r['exposure_cap_skips']>0


def test_load_funding_cache_from_user_zip(tmp_path):
    import zipfile
    source='/mnt/data/LIGHTER_MODE_GRID_TP4_TO_TP7_FUNDING_RESULTS.zip'
    if not Path(source).exists():
        pytest.skip('Original uploaded public funding ZIP not available')
    with zipfile.ZipFile(source) as z:
        path=tmp_path/'funding.csv'
        path.write_bytes(z.read('funding_hourly_1h.csv'))
    idx=v.load_funding_cache(path)
    assert len(idx['XAU'])==191
    assert len(idx['PAXG'])==191
    assert all(t%3600000==0 for t in idx['XAU'])


def test_end_to_end_report_36_fee_scenarios_with_synthetic_stream(tmp_path,monkeypatch):
    t,e,cfg=scenario_no_exit()
    e += [trade(t+12500,99.5,'A'),x(t+14500,102.5,102.6),
          x(t+15500,98.9,99.),p(t+15500,100.5,100.6)]
    history=ordered(e)
    fake_manifest={'snapshots_per_symbol':{'XAU':1,'PAXG':1},'source_complete':True}
    monkeypatch.setattr(v,'get_manifest',lambda root:({'XAU':[],'PAXG':[]},fake_manifest))
    monkeypatch.setattr(v,'calibration_distribution',lambda *args:({'center_usd_oz':2.,
                             'n_sampled_minutes':10000},[{'lower_usd_oz':2.,'upper_usd_oz':3.,'minutes':10000}]))
    monkeypatch.setattr(v,'load_taker_tape',lambda *args:([SimpleNamespace(ts=t+12500)],{'taker_trades':1}))
    def fake_merge(root,records,tape,counters):
        counters['XAU_rows_seen']=counters['PAXG_rows_seen']=1
        counters['XAU_parsed']=counters['PAXG_parsed']=1
        return iter(history)
    monkeypatch.setattr(v,'merge_timeline',fake_merge)
    output=tmp_path/'output'
    report=v.run('unused','unused',output,qtys=(.5,1.),tps=(2.,3.,4.,5.,6.,7.),cfg=cfg)
    assert len(report['scenarios'])==36
    assert (output/'summary.json').is_file()
    assert (output/'closed_trades.csv').is_file()
    assert (output/'order_event_audit.csv').is_file()
    import csv
    with (output/'closed_trades.csv').open() as f:
        rows=list(csv.DictReader(f))
    assert rows
    assert all('grid_level_usd_oz' in r and 'actual_entry_spread_usd_oz' in r
               and 'target_exit_spread_usd_oz' in r for r in rows)


def test_long_nominal_grid_minus7_actual_minus7_5_tp4_targets_minus3_5():
    assert v.tp_spread_target('long',-7.5,4.)==-3.5
    assert v.exit_reached('long',-3.5,-3.5)
    assert not v.exit_reached('long',-4.,-3.5)
    assert v.cancel_required('long',-5.99,-7.,1.)
    assert not v.cancel_required('long',-6.,-7.,1.)


def test_strict_exact_only_disables_optimistic_price_through():
    t,e,cfg=scenario_no_exit()
    cfg=v.Config(**{**vars(cfg),'through_fills':False})
    e += [trade(t+12500,99.5,'A')]
    result,closed,_,_,_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                         test_start=t,test_end=t+20000)
    assert result['maker_posts']==1
    assert result['maker_fill_events']==0
    assert not closed


def test_partial_then_fill_during_cancel_generates_unique_hedged_deltas():
    t,e,cfg=scenario_no_exit(cancel_latency=2500)
    e=[r for r in e if r[0]<=t+13000]
    e += [trade(t+12500,100.,'A',.11), # .01 ahead, then .10 filled partial
          trade(t+13000,99.9,'A',.01), # through fills remaining while cancel pending
          x(t+15000,102.5,102.6)]
    r,closed,terminal,audit,grids=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                                                 test_start=t,test_end=t+17000,record_events=True)
    assert r['maker_fill_events']==2
    assert r['fills_during_cancel_race']==1
    assert r['xau_hedges']==2
    hedged=[q for q in audit if q['event']=='XAU_HEDGE']
    assert len({row['lot_id'] for row in hedged})==2
    assert sum(lot.qty for lot in grids['short_1'].lots)==pytest.approx(.5)


def test_post_budget_enforces_maximum_requests_per_rolling_minute():
    t,e,cfg=scenario_no_exit()
    cfg=v.Config(**{**vars(cfg),'pending_cap':10,'post_per_minute':1})
    e=[]
    for i in range(1,16):
        e.extend([x(t+i*1000,105.2,105.3),p(t+i*1000,100.,100.1)])
    r,*_=v.paper_replay(ordered(e),2.,1.,.5,4.,cfg,
                        test_start=t,test_end=t+17000)
    assert r['maker_posts']==1
    assert r['post_rate_limited']>0
