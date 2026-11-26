#!/usr/bin/env python3
"""OFFLINE ONLY. Mode-centered Lighter XAU/PAXG grid backtest, execution vNext.

7d modal histogram center; frozen grid step. Side-specific executable entry
reference; 10s uninterrupted qualification; $1 cancellation hysteresis;
PAXG maker simulated POST/CANCEL ACK and in-flight cancel fills; optional
2 pending makers; XAU hedge from a book >= maker fill + 2s; TP anchored to
ACTUAL XAU-HEDGE MINUS PAXG-FILL spread, never nominal grid level.

Conservative caveat: exchange order acceptance, queue priority, partial fills,
exit atomicity and rate quotas cannot be proved from public 1s L2/tape.
No trading, credentials, network calls, or modification of source data.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from scripts.backtest_lighter_mode_grid_1s import (
    TRAIN_END, TEST_END, HEDGE_LATENCY_MS, EPS,
    calibration_distribution, executable, modal_histogram_center,
    spread_refs, zones_for_observed, qualifies, pnl_per_oz,
    fee_reprice, write_csv,
)
from scripts.analyze_lighter_spread_feasibility import iso, ms
from scripts.forwardtest_lighter_spread_grid_15d import FEE_PROFILES
from scripts.replay_lighter_causal_maker_first_15d import load_taker_tape
from scripts.replay_lighter_1s_frozen_tp2_tp3 import get_manifest, merge_timeline


@dataclass(frozen=True)
class Config:
    dwell_ms: int = 10_000
    cancel_buffer: float = 1.0
    post_latency_ms: int = 1000       # EXPERIMENTAL, NOT measured from live account
    cancel_latency_ms: int = 1000     # EXPERIMENTAL, NOT measured from live account
    hedge_latency_ms: int = 2000     # user-chosen paper assumption, never a live delay
    pending_cap: int = 2             # provisional, total across both directions
    post_per_minute: int = 20        # simulator budget, NOT Lighter tier limit
    cancel_per_minute: int = 30      # simulator budget, NOT Lighter tier limit
    max_age_ms: int = 3000
    max_signal_gap_ms: int = 2000
    max_levels: int = 64
    max_filled_exposure_oz: float = 6.0  # research guardrail, not live-risk approval
    through_fills: bool = True       # optimistic counterfactual sensitivity
    snapshot_dd_every_ms: int = 60_000
    reset_requires_center_revisit: bool = True # do not recycle Grid +3 on a tiny pullback

    def validate(self):
        if any(getattr(self, k) < 0 for k in ('dwell_ms','cancel_buffer', 'post_latency_ms',
                                               'cancel_latency_ms','hedge_latency_ms')):
            raise ValueError('negative dwell/buffer/latency')
        if min(self.pending_cap,self.post_per_minute,self.cancel_per_minute,
               self.max_age_ms,self.max_signal_gap_ms,self.max_levels) < 1:
            raise ValueError('invalid cap/age/level setting')
        if not (math.isfinite(self.max_filled_exposure_oz) and self.max_filled_exposure_oz > 0):
            raise ValueError('invalid maximum exposure')
        return self


def cancel_required(side, value, grid_level, buffer_usd):
    """Strict hysteresis. At equality, pending Maker stays active."""
    if side == 'short':
        return value < grid_level - buffer_usd - EPS
    if side == 'long':
        return value > grid_level + buffer_usd + EPS
    raise ValueError(side)


def entry_spread(xau_hedge_price, paxg_maker_fill_price):
    """Actual filled spread, independent of the signal's nominal grid level."""
    return xau_hedge_price - paxg_maker_fill_price


def tp_spread_target(side, actual_entry, tp_usd_oz):
    if not math.isfinite(actual_entry) or tp_usd_oz <= 0:
        raise ValueError('invalid actual entry or TP')
    if side == 'short':
        return actual_entry - tp_usd_oz
    if side == 'long':
        return actual_entry + tp_usd_oz
    raise ValueError(side)


def exit_reached(side, observed_exit_spread, target_spread):
    if side == 'short':
        return observed_exit_spread <= target_spread + EPS
    if side == 'long':
        return observed_exit_spread >= target_spread - EPS
    raise ValueError(side)


@dataclass
class MakerOrder:
    order_id: str
    grid_id: str
    side: str
    limit: float
    requested_qty: float
    remaining_qty: float
    sent_ms: int
    ack_due_ms: int
    state: str = 'POSTING'  # POSTING, RESTING, CANCEL_QUEUED, CANCELING, CANCELLED, REJECTED, FILLED
    ack_ms: int | None = None
    cancel_sent_ms: int | None = None
    cancel_due_ms: int | None = None
    cancel_on_ack: bool = False


@dataclass
class Lot:
    lot_id: str
    qty: float
    entry_p: float
    maker_fill_ms: int
    fill_evidence: str
    maker_order_id: str
    entry_x: float | None = None
    hedge_ms: int | None = None


@dataclass
class Level:
    side: str
    number: int
    threshold: float
    order: MakerOrder | None = None
    lots: list[Lot] = field(default_factory=list)
    qualify_since_ms: int | None = None
    last_qualify_ms: int | None = None
    rearm_after_close: bool = False

    @property
    def key(self):
        return f'{self.side}_{self.number}'


@dataclass
class QueueGroup:
    side: str
    price: float
    ahead: float
    order_ids: list[str] = field(default_factory=list)


class RollingBudget:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.calls = deque()
    def permit(self, t):
        while self.calls and t - self.calls[0] >= 60_000:
            self.calls.popleft()
        if len(self.calls) >= self.per_minute:
            return False
        self.calls.append(t)
        return True


def paper_replay(timeline, center, step, qty, tp, config: Config,
                 *, test_start=TRAIN_END, test_end=TEST_END, record_events=False):
    cfg = config.validate()
    if not (step > 0 and qty > 0 and tp > 0):
        raise ValueError('invalid grid-step/quantity/TP')
    grids = {}
    groups = {}
    orders = {}
    stats = Counter()
    audit, closed = [], []
    latest = {'XAU':None,'PAXG':None}
    post_budget = RollingBudget(cfg.post_per_minute)
    cancel_budget = RollingBudget(cfg.cancel_per_minute)
    order_seq, lot_seq = 0, 0
    last_event = None
    last_dd_sample = None
    realized_gross = 0.0
    equity_peak = 0.0
    max_drawdown = 0.0
    max_exposure_oz = 0.0
    max_active_makers = 0
    max_hedge_delay = 0

    def trace(kind, t, level=None, **extra):
        if record_events:
            audit.append({'event':kind,'time_utc':iso(t),
                          'grid':level.key if level else '',
                          'grid_level_usd_oz':level.threshold if level else None,
                          **extra})

    def active_makers():
        return sum(level.order is not None and level.order.state not in
                   ('FILLED','CANCELLED','REJECTED') for level in grids.values())

    def exposure_oz():
        return sum(lot.qty for level in grids.values() for lot in level.lots)

    def remove_order(order):
        key = (order.side,round(order.limit, 6))
        group = groups.get(key)
        if group:
            group.order_ids = [name for name in group.order_ids if name != order.order_id]
            if not group.order_ids:
                del groups[key]
        level = grids[order.grid_id]
        if level.order is order:
            level.order = None
        orders.pop(order.order_id, None)

    def submit_cancel(level, t, reason):
        order = level.order
        if order is None:
            return
        if order.state == 'POSTING':
            order.cancel_on_ack = True
            stats['cancel_requested_while_posting'] += 1
            trace('CANCEL_DEFERRED_UNTIL_POST_ACK',t,level,order_id=order.order_id,reason=reason)
            return
        if order.state in ('CANCEL_QUEUED','CANCELING'):
            return
        if order.state != 'RESTING':
            raise AssertionError(f'unexpected order cancel {order.state}')
        if cancel_budget.permit(t):
            order.state = 'CANCELING'
            order.cancel_sent_ms = t
            order.cancel_due_ms = t+cfg.cancel_latency_ms
            stats['cancel_requests'] += 1
            trace('CANCEL_SEND',t,level,order_id=order.order_id,reason=reason,
                  remaining_qty_oz=order.remaining_qty)
        else:
            order.state = 'CANCEL_QUEUED'
            stats['cancel_throttled'] += 1
            trace('CANCEL_QUEUED',t,level,order_id=order.order_id,reason=reason)

    def move_queued_cancel(t):
        # Pending cancels take priority over all new creates.
        for level in sorted(grids.values(),key=lambda x:(x.number,x.side)):
            order = level.order
            if order and order.state == 'CANCEL_QUEUED':
                if not cancel_budget.permit(t):
                    return
                order.state = 'CANCELING'
                order.cancel_sent_ms = t
                order.cancel_due_ms = t+cfg.cancel_latency_ms
                stats['cancel_requests'] += 1
                trace('CANCEL_SEND_AFTER_QUEUE',t,level,order_id=order.order_id,
                      remaining_qty_oz=order.remaining_qty)

    def advance_acks(t, event_kind):
        # Simulated ACK on source events after configured latency. Not actual matching-engine time.
        # On tie with public trade, CANCEL ACK occurs after that trade (conservative cancel race).
        for order in list(orders.values()):
            level=grids[order.grid_id]
            if order.state == 'POSTING' and t >= order.ack_due_ms:
                p=latest['PAXG']
                if (p is None or p.ts>t or t-p.ts>cfg.max_age_ms):
                    order.state='REJECTED'
                    stats['post_ack_missing_paxg_quote']+=1
                    trace('POST_REJECT_STALE_QUOTE',t,level,order_id=order.order_id)
                    remove_order(order)
                    continue
                # Post-only cannot become a taker during the in-flight submit.
                if (order.side == 'BUY' and order.limit >= p.ask[0][0]-EPS or
                    order.side == 'SELL' and order.limit <= p.bid[0][0]+EPS):
                    order.state='REJECTED'
                    stats['post_only_reject']+=1
                    trace('POST_ONLY_REJECT',t,level,order_id=order.order_id)
                    remove_order(order)
                    continue
                order.state='RESTING'
                order.ack_ms=t
                key=(order.side,round(order.limit,6))
                if key not in groups:
                    visible=p.bid[0][1] if order.side=='BUY' else p.ask[0][1]
                    groups[key]=QueueGroup(order.side,order.limit,float(visible))
                groups[key].order_ids.append(order.order_id)
                stats['post_acks']+=1
                trace('POST_ACK',t,level,order_id=order.order_id,limit=order.limit,
                      queue_ahead_oz=groups[key].ahead)
                if order.cancel_on_ack:
                    submit_cancel(level,t,'BREACHED_WHILE_POSTING')
            if (order.state=='CANCELING' and t>=order.cancel_due_ms and
                not (event_kind==2 and t==order.cancel_due_ms)):
                order.state='CANCELLED'
                stats['cancel_acks']+=1
                trace('CANCEL_ACK',t,level,order_id=order.order_id,
                      remaining_qty_oz=order.remaining_qty)
                remove_order(order)
        move_queued_cancel(t)

    def on_fill(order, quantity, t, evidence):
        nonlocal lot_seq
        level=grids[order.grid_id]
        amount=min(quantity,order.remaining_qty)
        if amount<=EPS or order.state not in ('RESTING','CANCEL_QUEUED','CANCELING'):
            return 0.0
        order.remaining_qty=max(0.,order.remaining_qty-amount)
        lot_seq+=1
        lot=Lot(f'{order.order_id}-fill{lot_seq}',amount,order.limit,t,evidence,order.order_id)
        level.lots.append(lot)
        # One maker Fill consumes this Grid number even if actual Fill spread
        # slips below its threshold. The next deeper Grid remains independent.
        level.rearm_after_close = True
        stats['maker_fill_events']+=1
        stats['maker_fill_milli_oz']+=round(amount*1000)
        stats['through_fill_events' if evidence=='through' else 'exact_fill_events']+=1
        if order.state in ('CANCELING','CANCEL_QUEUED'):
            stats['fills_during_cancel_race']+=1
        trace('MAKER_PAPER_FILL',t,level,order_id=order.order_id,lot_id=lot.lot_id,
              actual_paxg_fill_price=order.limit,filled_qty_oz=amount,evidence=evidence,
              hedge_eligible_utc=iso(t+cfg.hedge_latency_ms))
        if order.remaining_qty<=EPS:
            order.state='FILLED'
            remove_order(order)
        else:
            # Explicit research policy: do NOT leave remainder hanging after partial.
            submit_cancel(level,t,'PARTIAL_FILL_REMAINDER')
        return amount

    def on_trade(t, trade):
        side='BUY' if trade.taker_side=='A' else 'SELL'
        for key,group in list(groups.items()):
            if group.side!=side:
                continue
            # BUY maker is swept by taker SELL at/below limit; SELL vice versa.
            if side=='BUY' and trade.price>group.price+EPS:continue
            if side=='SELL' and trade.price<group.price-EPS:continue
            eligible=[]
            for name in list(group.order_ids):
                order=orders.get(name)
                if (order and order.state in ('RESTING','CANCELING','CANCEL_QUEUED')
                    and order.ack_ms is not None and order.ack_ms<t):
                    eligible.append(order)
            if not eligible:continue
            stats['eligible_public_prints']+=1
            is_through=abs(trade.price-group.price)>EPS
            if is_through:
                stats['through_public_prints']+=1
                if cfg.through_fills:
                    for order in eligible:
                        # Same documented optimistic price-through counterfactual
                        # as baseline, NOT proof of actual accepted order/queue.
                        on_fill(order,order.remaining_qty,t,'through')
                continue
            available=float(trade.qty)
            consume=min(available,group.ahead)
            group.ahead-=consume
            available-=consume
            if available<=EPS:continue
            for order in eligible:
                if available<=EPS:break
                done=on_fill(order,min(available,order.remaining_qty),t,'exact')
                available-=done

    def on_xau(t,x):
        nonlocal max_hedge_delay
        used={}
        for level in grids.values():
            for lot in level.lots:
                if lot.hedge_ms is not None or t<lot.maker_fill_ms+cfg.hedge_latency_ms:
                    continue
                side='sell' if level.side=='short' else 'buy'
                px=executable(x,side,lot.qty,used)
                if px is None:
                    stats['hedge_depth_unavailable']+=1
                    continue
                lot.entry_x=px
                lot.hedge_ms=t
                delay=t-lot.maker_fill_ms
                max_hedge_delay=max(max_hedge_delay,delay)
                stats['hedges']+=1
                actual=entry_spread(px,lot.entry_p)
                target=tp_spread_target(level.side,actual,tp)
                trace('XAU_HEDGE',t,level,lot_id=lot.lot_id,
                      xau_entry_px=px,paxg_entry_px=lot.entry_p,
                      actual_entry_spread_usd_oz=actual,tp_distance_usd_oz=tp,
                      target_exit_spread_usd_oz=target,hedge_delay_ms=delay)

    def pair_refs(t):
        x,p=latest['XAU'],latest['PAXG']
        if (x is None or p is None or x.ts>t or p.ts>t or
            t-x.ts>cfg.max_age_ms or t-p.ts>cfg.max_age_ms):
            return None
        return spread_refs(x,p,qty)

    def evaluate(t):
        nonlocal realized_gross,last_dd_sample,equity_peak,max_drawdown
        nonlocal max_exposure_oz,max_active_makers,order_seq
        refs=pair_refs(t)
        if refs is None:
            stats['invalid_or_stale_signal']+=1
            for level in grids.values():
                level.qualify_since_ms=None
                level.last_qualify_ms=None
            return
        x,p=latest['XAU'],latest['PAXG']
        zones_for_observed(center,step,refs,grids,cfg.max_levels)
        # We reuse base grid creation; Level is a distinct state model, convert once.
        for key,level in list(grids.items()):
            if not isinstance(level,Level):
                grids[key]=Level(level.side,level.number,level.level)
        # FIRST: TP from EXECUTED entry spread, not nominal grid. Never close on the hedge tick.
        exit_used_x,exit_used_p={},{}
        for level in grids.values():
            if not level.lots:continue
            remaining_lots=[]
            for lot in level.lots:
                if lot.hedge_ms is None or t<=lot.hedge_ms:
                    remaining_lots.append(lot)
                    continue
                side_x='buy' if level.side=='short' else 'sell'
                side_p='sell' if level.side=='short' else 'buy'
                ux,up=dict(exit_used_x),dict(exit_used_p)
                xp=executable(x,side_x,lot.qty,ux)
                pp=executable(p,side_p,lot.qty,up)
                if xp is None or pp is None:
                    stats['insufficient_exit_depth']+=1
                    remaining_lots.append(lot)
                    continue
                actual=entry_spread(lot.entry_x,lot.entry_p)
                target=tp_spread_target(level.side,actual,tp)
                exit_value=entry_spread(xp,pp)
                if not exit_reached(level.side,exit_value,target):
                    remaining_lots.append(lot)
                    continue
                exit_used_x,exit_used_p=ux,up
                pnl=pnl_per_oz(level.side,lot.entry_p,lot.entry_x,pp,xp)
                if pnl+EPS<tp:
                    raise AssertionError('TP triggered without required actual gain')
                row={'side':level.side,'grid':level.key,
                     'grid_level_usd_oz':level.threshold,
                     'actual_entry_spread_usd_oz':actual,
                     'tp_distance_usd_oz':tp,
                     'target_exit_spread_usd_oz':target,
                     'actual_exit_spread_usd_oz':exit_value,
                     'slippage_vs_grid_usd_oz': actual-level.threshold,
                     'qty_oz':lot.qty,
                     'paxg_entry_px':lot.entry_p,'xau_entry_px':lot.entry_x,
                     'paxg_exit_px':pp,'xau_exit_px':xp,
                     'maker_fill_utc':iso(lot.maker_fill_ms),
                     'hedge_utc':iso(lot.hedge_ms),'exit_utc':iso(t),
                     'fill_evidence':lot.fill_evidence,'order_id':lot.maker_order_id,
                     'gross_per_oz_usd':pnl,'gross_usd':lot.qty*pnl}
                closed.append(row)
                realized_gross+=row['gross_usd']
                stats['tp_closes']+=1
                trace('TP_CLOSE',t,level,lot_id=lot.lot_id,
                      actual_entry_spread_usd_oz=actual,
                      tp_target_spread_usd_oz=target,realized_exit_spread_usd_oz=exit_value,
                      gross_per_oz_usd=pnl)
            level.lots=remaining_lots
            if not level.lots and level.order is None:
                level.rearm_after_close=True
        # SECOND: evaluate cancel requests for resting orders (strict $1 buffer).
        for level in grids.values():
            ref=refs[level.side]
            if ref is None:continue
            # After a real/paper Fill the Grid is consumed. Under the default
            # policy it can only re-arm after its lots/orders are gone AND the
            # reference returns to the modal center; not on a tiny pullback
            # below the trigger which would repeatedly reuse Grid +3.
            if (level.rearm_after_close and not level.lots and level.order is None):
                if cfg.reset_requires_center_revisit:
                    rearmed = (ref <= center+EPS if level.side=='short'
                               else ref >= center-EPS)
                else:
                    rearmed = not qualifies(level.side,ref,level.threshold)
                if rearmed:
                    level.rearm_after_close=False
            if level.order and cancel_required(level.side,ref,level.threshold,cfg.cancel_buffer):
                submit_cancel(level,t,'SPREAD_BREACHED_BUFFER')
        move_queued_cancel(t)
        # THIRD: qualifying signal timer and new orders. Higher priority = lower grid
        # ordinal, so +3's slot is occupied before +4's in a multi-level move.
        ready=[]
        for level in grids.values():
            ref=refs[level.side]
            if (ref is None or level.order is not None or level.lots or level.rearm_after_close):
                level.qualify_since_ms=None; level.last_qualify_ms=None
                continue
            if not qualifies(level.side,ref,level.threshold):
                level.qualify_since_ms=None; level.last_qualify_ms=None
                continue
            if (level.last_qualify_ms is None or
                t-level.last_qualify_ms>cfg.max_signal_gap_ms or
                level.qualify_since_ms is None):
                level.qualify_since_ms=t
            level.last_qualify_ms=t
            if t-level.qualify_since_ms >= cfg.dwell_ms:
                ready.append(level)
        ready.sort(key=lambda z:(z.number, 0 if z.side=='short' else 1))
        for level in ready:
            if active_makers() >= cfg.pending_cap:
                stats['pending_cap_skips']+=1
                continue
            # Include all orders still capable of filling (even CANCELING),
            # otherwise many resting orders can overflow the position cap.
            potential_pending = sum(z.order.remaining_qty for z in grids.values()
                                    if z.order is not None)
            if exposure_oz()+potential_pending+qty > cfg.max_filled_exposure_oz+EPS:
                stats['exposure_cap_skips']+=1
                continue
            if any(l.order and l.order.state=='CANCEL_QUEUED' for l in grids.values()):
                stats['create_blocked_by_cancel_queue']+=1
                continue
            if not post_budget.permit(t):
                stats['post_rate_limited']+=1
                continue
            side='BUY' if level.side=='short' else 'SELL'
            limit=p.bid[0][0] if side=='BUY' else p.ask[0][0]
            order_seq+=1
            oid=f'paper-{order_seq}-{level.key}'
            order=MakerOrder(oid,level.key,side,limit,qty,qty,t,t+cfg.post_latency_ms)
            level.order=order
            orders[oid]=order
            level.qualify_since_ms=None;level.last_qualify_ms=None
            stats['maker_posts']+=1
            trace('POST_SEND',t,level,order_id=oid,maker_action=side,
                  maker_limit=limit,reference_spread_usd_oz=refs[level.side],
                  post_ack_target_utc=iso(order.ack_due_ms))
        max_active_makers=max(max_active_makers,active_makers())
        max_exposure_oz=max(max_exposure_oz,exposure_oz())
        if last_dd_sample is None or t-last_dd_sample>=cfg.snapshot_dd_every_ms:
            last_dd_sample=t
            ux,up={},{}
            mark=0.0
            full=True
            for level in grids.values():
                for lot in level.lots:
                    side_p='sell' if level.side=='short' else 'buy'
                    ppx=executable(p,side_p,lot.qty,up)
                    if ppx is None:
                        full=False;break
                    if lot.hedge_ms is None:
                        mark+=(ppx-lot.entry_p)*lot.qty if level.side=='short' else (lot.entry_p-ppx)*lot.qty
                    else:
                        side_x='buy' if level.side=='short' else 'sell'
                        xpx=executable(x,side_x,lot.qty,ux)
                        if xpx is None:full=False;break
                        mark+=pnl_per_oz(level.side,lot.entry_p,lot.entry_x,ppx,xpx)*lot.qty
                if not full:break
            if full:
                eq=realized_gross+mark
                equity_peak=max(equity_peak,eq)
                max_drawdown=max(max_drawdown,equity_peak-eq)
                stats['complete_marks']+=1
            else:
                stats['incomplete_marks']+=1

    for t,kind,obj in timeline:
        if t>=test_end:break
        if t<test_start:continue
        last_event=t
        if kind==0:
            latest['XAU']=obj
            if obj is not None:
                on_xau(t,obj)
        elif kind==1:
            latest['PAXG']=obj
        elif kind!=2:
            raise ValueError('unexpected event kind')
        # For trade exact cancel-ACK boundary, allow that print to race.
        advance_acks(t,kind)
        if kind==2:
            on_trade(t,obj)
        elif obj is not None:
            evaluate(t)
        else:
            stats['invalid_book_events']+=1
            for level in grids.values():
                level.qualify_since_ms=None
                level.last_qualify_ms=None

    if last_event is None:
        raise ValueError('Empty test interval')
    terminal=[]
    ux,up={},{}
    x,p=latest['XAU'],latest['PAXG']
    if x and last_event-x.ts>cfg.max_age_ms:x=None
    if p and last_event-p.ts>cfg.max_age_ms:p=None
    for level in grids.values():
        for lot in level.lots:
            pxp=None;pxx=None
            side_p='sell' if level.side=='short' else 'buy'
            side_x='buy' if level.side=='short' else 'sell'
            if p is not None:
                pxp=executable(p,side_p,lot.qty,up)
            if x is not None and lot.hedge_ms is not None:
                pxx=executable(x,side_x,lot.qty,ux)
            if pxp is None or (lot.hedge_ms is not None and pxx is None):
                gross=None;status='UNMARKED'
            elif lot.hedge_ms is None:
                gross=(pxp-lot.entry_p)*lot.qty if level.side=='short' else (lot.entry_p-pxp)*lot.qty
                status='UNHEDGED'
            else:
                gross=pnl_per_oz(level.side,lot.entry_p,lot.entry_x,pxp,pxx)*lot.qty
                status='OPEN_HEDGED'
            actual=entry_spread(lot.entry_x,lot.entry_p) if lot.entry_x is not None else None
            terminal.append({'grid':level.key,'side':level.side,
                'grid_level_usd_oz':level.threshold,
                'actual_entry_spread_usd_oz':actual,
                'tp_distance_usd_oz':tp,
                'target_exit_spread_usd_oz':tp_spread_target(level.side,actual,tp) if actual is not None else None,
                'qty_oz':lot.qty,'status':status,'gross_usd':gross,
                'paxg_entry_px':lot.entry_p,'xau_entry_px':lot.entry_x,
                'paxg_exit_px':pxp,'xau_exit_px':pxx,
                'maker_fill_utc':iso(lot.maker_fill_ms),
                'hedge_utc':iso(lot.hedge_ms) if lot.hedge_ms is not None else '',
                'order_id':lot.maker_order_id,'fill_evidence':lot.fill_evidence})
    result={'qty_per_grid_oz':qty,'tp_gross_usd_per_oz':tp,
        'center_usd_oz':center,'grid_step_usd_oz':step,
        'maker_posts':stats['maker_posts'],'maker_post_acks':stats['post_acks'],
        'post_only_rejections':stats['post_only_reject'],
        'maker_cancel_requests':stats['cancel_requests'],'maker_cancel_acks':stats['cancel_acks'],
        'maker_fill_events':stats['maker_fill_events'],
        'paper_filled_oz':stats['maker_fill_milli_oz']/1000,
        'exact_fills':stats['exact_fill_events'],'through_fills':stats['through_fill_events'],
        'fills_during_cancel_race':stats['fills_during_cancel_race'],
        'xau_hedges':stats['hedges'],'tp_closes':stats['tp_closes'],
        'max_hedge_delay_ms':max_hedge_delay,'pending_at_end':active_makers(),
        'terminal_open_hedged':sum(l.hedge_ms is not None for g in grids.values() for l in g.lots),
        'terminal_unhedged_paxg':sum(l.hedge_ms is None for g in grids.values() for l in g.lots),
        'max_active_makers':max_active_makers,'max_paxg_exposure_oz':max_exposure_oz,
        'cancel_throttled':stats['cancel_throttled'],
        'post_rate_limited':stats['post_rate_limited'],
        'pending_cap_skips':stats['pending_cap_skips'],
        'exposure_cap_skips':stats['exposure_cap_skips'],
        'post_ack_missing_paxg_quote':stats['post_ack_missing_paxg_quote'],
        'cancel_requested_while_posting':stats['cancel_requested_while_posting'],
        'max_sampled_fee_free_mark_drawdown_usd':max_drawdown if stats['complete_marks'] else None,
        'incomplete_equity_marks':stats['incomplete_marks'],
        'invalid_book_events':stats['invalid_book_events'],
        'closed_gross_usd':realized_gross}
    # Return state for auditing / test: each grid's immutable threshold and fill history.
    return result,closed,terminal,audit,grids


def load_funding_cache(path):
    """Use only verified public hourly USD/unit rows from the earlier LOCAL cache.

    If absent/incomplete, funding-adjusted P&L is UNKNOWN instead of zero.
    """
    if not path or not Path(path).is_file():
        return None
    idx={'XAU':{},'PAXG':{}}
    with Path(path).open(newline='',encoding='utf8') as f:
        for r in csv.DictReader(f):
            if (r.get('source')!='Lighter public /api/v1/fundings resolution=1h'
                or r.get('symbol') not in idx):
                raise ValueError('Funding cache lacks official hourly provenance')
            t=int(r['settlement_ms']); symbol=r['symbol']
            # Signed USD/base-unit, not raw 10s rates.
            value=float(r['signed_usd_per_unit'])
            if not math.isfinite(value) or t%3_600_000:
                raise ValueError('Invalid funding hourly rate')
            if t in idx[symbol] and idx[symbol][t] != value:
                raise ValueError('Conflicting funding settlement')
            idx[symbol][t]=value
    return idx


def funding_for_position(row, index, test_end):
    if index is None:
        return None
    start=ms(row['maker_fill_utc'])
    end=ms(row.get('exit_utc') or test_end)
    hedge=ms(row['hedge_utc']) if row.get('hedge_utc') else None
    if end<=start:raise ValueError('Invalid position time')
    total=0.0
    for t in range((start//3_600_000+1)*3_600_000,end,3_600_000):
        symbols=['PAXG']+(['XAU'] if hedge is not None and hedge<t else [])
        for symbol in symbols:
            value=index[symbol].get(t)
            if value is None:return None
            leg_long=(row['side']=='short') if symbol=='PAXG' else (row['side']=='long')
            total+=float(row['qty_oz'])*value*(-1 if leg_long else 1)
    return total


def fee_and_funding(base,closed,terminal,fees,funding_index):
    maker_bps,taker_bps=fees
    actual_closed=[];actual_terminal=[]
    total_fee=0.0;total_funding=0.0;funding_complete=funding_index is not None
    for source, destination in ((closed,actual_closed),(terminal,actual_terminal)):
        for row in source:
            r=dict(row)
            if r['gross_usd'] is not None:
                r['fee_only_net_usd'],r['fees_usd']=fee_reprice(r,maker_bps,taker_bps)
                total_fee+=r['fees_usd']
            else:
                r['fee_only_net_usd']=None;r['fees_usd']=None
            cash=funding_for_position(r,funding_index,TEST_END) if funding_index is not None else None
            r['funding_usd']=cash
            if cash is None:
                funding_complete=False
            else:
                total_funding+=cash
            r['fee_funding_net_usd']=(r['fee_only_net_usd']+cash
                                     if r['fee_only_net_usd'] is not None and cash is not None else None)
            destination.append(r)
    all_rows=actual_closed+actual_terminal
    if any(r['fee_only_net_usd'] is None for r in all_rows):
        net_fee_only=None
    else:
        net_fee_only=round(sum(r['fee_only_net_usd'] for r in all_rows),6)
    net_funding=(round(net_fee_only+total_funding,6)
                 if net_fee_only is not None and funding_complete else None)
    return {**base,'total_fee_usd':round(total_fee,6),
            'fee_only_net_with_terminal_usd':net_fee_only,
            'funding_usd':round(total_funding,6) if funding_complete else None,
            'net_after_fees_funding_terminal_usd':net_funding,
            'funding_status':'PUBLIC_HOURLY_WITH_ASSUMPTIONS' if funding_complete else 'UNVERIFIED_MISSING_OR_INCOMPLETE'},actual_closed,actual_terminal


def run(source_1s, source_old, output, *, histogram_bin=1.,step=1.,
        qtys=(.5,1.), tps=(2.,3.,4.,5.,6.,7.),cfg=Config(),
        funding_csv=None):
    cfg.validate()
    root,old,out=Path(source_1s),Path(source_old),Path(output)
    out.mkdir(parents=True,exist_ok=True)
    print('VERIFY_SOURCE_AND_CALIBRATE_7D',flush=True)
    records,manifest=get_manifest(root)
    center_meta,hist=calibration_distribution(root,records,histogram_bin,cfg.max_age_ms)
    center=center_meta['center_usd_oz']
    print('GRID_CENTER',center,'MEAN_NA SD_NA PERCENTILES_NA',flush=True)
    tape,tape_quality=load_taker_tape(old)
    tape=[item for item in tape if TRAIN_END<=item.ts<TEST_END]
    if not tape:
        raise RuntimeError('Missing PAXG public taker tape; zero fills would be unreliable')
    funding=load_funding_cache(funding_csv)
    paths=[];all_closed=[];all_terminal=[];audit=[];levels=[]
    data_quality=None
    for qty in qtys:
        for tp in tps:
            counters=Counter()
            print('REPLAY_VNEXT',qty,tp,'dwell_ms',cfg.dwell_ms,'buffer',cfg.cancel_buffer,
                  'pending_cap',cfg.pending_cap,flush=True)
            result,closed,terminal,events,grids=paper_replay(
                merge_timeline(root,records,tape,counters),center,step,qty,tp,cfg,
                record_events=(qty==qtys[0] and tp==tps[0]))
            observed={s:{'raw':counters[s+'_rows_seen'],'valid':counters[s+'_parsed'],
                         'invalid':counters[s+'_rejected']} for s in ('XAU','PAXG')}
            for sym in ('XAU','PAXG'):
                if observed[sym]['raw'] != manifest['snapshots_per_symbol'][sym]:
                    raise RuntimeError('Missing/altered historical source rows for '+sym)
            if data_quality is None:data_quality=observed
            elif data_quality!=observed:raise RuntimeError('Source parsing varies between scenarios')
            print('PATH_RESULT',qty,tp,'posts',result['maker_posts'],'cancel_acks',
                  result['maker_cancel_acks'],'fills',result['maker_fill_events'],
                  'hedges',result['xau_hedges'],'closes',result['tp_closes'],flush=True)
            for label,fees in FEE_PROFILES.items():
                scenario,c,t=fee_and_funding(result,closed,terminal,fees,funding)
                scenario['fee_profile']=label
                paths.append(scenario)
                all_closed.extend([{**r,'fee_profile':label,'scenario_qty':qty,'scenario_tp':tp} for r in c])
                all_terminal.extend([{**r,'fee_profile':label,'scenario_qty':qty,'scenario_tp':tp} for r in t])
            if events:
                audit.extend([{**e,'scenario_qty':qty,'scenario_tp':tp} for e in events])
            levels.extend([{'qty':qty,'tp':tp,'grid':g.key,'side':g.side,
                            'grid_level_usd_oz':g.threshold} for g in grids.values()])
    report={'experiment':'MODAL_7D_GRID_VNEXT_ORDER_ACK_HYSTERESIS_ACTUAL_FILL_TP_OFFLINE',
        'study_interval_utc':[iso(TRAIN_END),iso(TEST_END)],
        'calibration':center_meta,'config':vars(cfg),
        'histogram_bin_usd_oz':histogram_bin,'grid_step_usd_oz':step,
        'qtys':list(qtys),'tp_usd_oz':list(tps),
        'funding_source':str(funding_csv) if funding is not None else None,
        'grid_rearm_policy':'Return to modal center after Fill+close' if cfg.reset_requires_center_revisit else 'Leave nominal trigger after Fill+close',
        'source_manifest':manifest,'test_quality':data_quality,
        'public_tape_quality':tape_quality,
        'scenarios':paths,
        'warnings':[
          'NO REAL LIVE ORDERS. Maker ACK/CANCEL ACK times and maker fills are SIMULATED. Price-through fills are conditional counterfactuals.',
          '10-second dwell, $1 buffer user-approved. Post/cancel 1000ms, pending cap 2, quota settings and $6oz max exposure are RESEARCH assumptions, not measured exchange behavior.',
          'TP target is based on XAU HEDGE FILL minus PAXG MAKER FILL, NEVER nominal grid threshold.',
          'Post-only guard based on last historical L2 and does not prove actual exchange acceptance.',
          'Maker exact-limit FIFO queue and price-through volume are approximations from public tape; no L3 order IDs.',
          'Backtest closes paired taker legs atomically on visible executable VWAP without exit latency. Do not use for live order routing.',
          'Funding optional verified local public-hourly CSV; missing hours produce unknown funding-adjusted PnL.',
          'Drawdown sampled fee-free and not full liquidation/margin model.',
          'Historical test window inspected previously; this is development, NOT untouched out-of-sample.'
        ]}
    write_csv(out/'histogram_7d.csv',hist,['lower_usd_oz','upper_usd_oz','minutes','fraction','modal_bin'])
    write_csv(out/'grid_levels.csv',levels,['grid','grid_level_usd_oz'])
    write_csv(out/'scenarios.csv',paths,['fee_profile','fee_only_net_with_terminal_usd'])
    write_csv(out/'closed_trades.csv',all_closed,['actual_entry_spread_usd_oz','target_exit_spread_usd_oz'])
    write_csv(out/'terminal_positions.csv',all_terminal,['actual_entry_spread_usd_oz','target_exit_spread_usd_oz'])
    write_csv(out/'order_event_audit.csv',audit,['event','grid','time_utc'])
    (out/'summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,default=list),encoding='utf8')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-1s',default='data/lighter_1s_20261122')
    p.add_argument('--old-source',default='data/lighter_fresh_20261122')
    p.add_argument('--output',default='data/lighter_fresh_20261122/MODE_GRID_VNEXT_1S')
    p.add_argument('--funding-csv',default='data/lighter_fresh_20261122/MODE_GRID_TP4_TO_TP7_FUNDING_1S/funding_hourly_1h.csv')
    p.add_argument('--hist-bin-usd',type=float,default=1.)
    p.add_argument('--grid-step-usd',type=float,default=1.)
    p.add_argument('--dwell-seconds',type=float,default=10.)
    p.add_argument('--cancel-buffer-usd',type=float,default=1.)
    p.add_argument('--post-latency-ms',type=int,default=1000)
    p.add_argument('--cancel-latency-ms',type=int,default=1000)
    p.add_argument('--hedge-latency-ms',type=int,default=2000)
    p.add_argument('--pending-cap',type=int,default=2)
    p.add_argument('--post-per-minute',type=int,default=20)
    p.add_argument('--cancel-per-minute',type=int,default=30)
    p.add_argument('--max-age-ms',type=int,default=3000)
    p.add_argument('--max-levels',type=int,default=64)
    p.add_argument('--max-exposure-oz',type=float,default=6.)
    p.add_argument('--strict-exact-only',action='store_true',help='Do NOT assume price-through maker fills')
    p.add_argument('--rearm-after-leaving-grid',action='store_true',help='Nondefault alternative to full center reset')
    p.add_argument('--qty',type=float,nargs='+',default=[.5,1.])
    p.add_argument('--tp',type=float,nargs='+',default=[2.,3.,4.,5.,6.,7.])
    a=p.parse_args()
    cfg=Config(dwell_ms=int(a.dwell_seconds*1000),cancel_buffer=a.cancel_buffer_usd,
        post_latency_ms=a.post_latency_ms,cancel_latency_ms=a.cancel_latency_ms,
        hedge_latency_ms=a.hedge_latency_ms,pending_cap=a.pending_cap,
        post_per_minute=a.post_per_minute,cancel_per_minute=a.cancel_per_minute,
        max_age_ms=a.max_age_ms,max_levels=a.max_levels,
        max_filled_exposure_oz=a.max_exposure_oz,through_fills=not a.strict_exact_only,
        reset_requires_center_revisit=not a.rearm_after_leaving_grid)
    run(a.source_1s,a.old_source,a.output,histogram_bin=a.hist_bin_usd,
        step=a.grid_step_usd,qtys=tuple(a.qty),tps=tuple(a.tp),cfg=cfg,
        funding_csv=a.funding_csv)

if __name__=='__main__':
    main()
