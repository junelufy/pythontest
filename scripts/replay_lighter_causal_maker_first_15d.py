#!/usr/bin/env python3
"""Conservative, causal historical L2 + PAXG public-tape maker-first simulation.

NOT an order execution record. Never call fills verified: public tape lacks actual
queue cancellations, hidden orders, order acknowledgement or precise live
latency. Existing 15-day holdout has been examined and is now a DEVELOPMENT
REPLAY, not an untouched forward validation set.

Event order: timestamped PAXG L2 -> post maker PAXG (no fill yet) ->
subsequent PAXG TAKER prints at the exact limit, consume visible queue ahead
(shared by ALL grids) -> partial PAXG fill -> hedge filled quantity using the
NEXT XAU L2 snapshot (never an older quote) -> TP from executable L2 prices.

Unfilled maker orders remain pending until the same frozen entry spread
returns outside that grid zone: SHORT cancels when ref < grid, LONG when
ref > grid. No arbitrary elapsed-time cancellation.

Frozen zones and TP 2/3 USD/oz remain unchanged. No stop loss or timeout for
HEDGED positions; only pending *unfilled* maker quotes expire after 5 minutes.
"""
import argparse
import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from scripts.analyze_lighter_spread_feasibility import Book, iso, ms, read_books, asof, vwap
from scripts.forwardtest_lighter_spread_grid_15d import (
    TEST_START, TEST_END, FEE_PROFILES, load_frozen_zones,
    current_reference, gross_oz, price_is_inside, price_is_outside, vwap_slice
)

# Snapshot freshness for evaluating spread conditions, not an order lifetime.
MAX_HEDGE_DELAY_MS = 5 * 60_000  # diagnostic only; no hedge-timeout cancellation
MAX_XAU_QUOTE_AGE_MS = 20_000
MAX_PAXG_QUOTE_AGE_MS = 20_000
PRICE_TOLERANCE = 0.011


@dataclass
class Tape:
    ts: int
    price: float
    qty: float
    taker_side: str      # "B" aggressor BUY, "A" aggressor SELL
    trade_id: str


@dataclass
class Order:
    grid_id: str
    direction: str
    qty: float
    limit: float
    placed_ms: int


@dataclass
class Group:
    maker_action: str    # BUY or SELL
    limit: float
    queue_remaining: float
    orders: list = field(default_factory=list)


@dataclass
class Lot:
    grid_id: str
    direction: str
    qty: float
    entry_p: float
    maker_fill_ms: int
    initial_order_qty: float
    entry_x: float | None = None
    x_hedge_ms: int | None = None
    entry_fee: float = 0.0


@dataclass
class State:
    direction: str
    label: str
    level: float
    armed: bool = False
    status: str = "flat"  # flat, pending, hedge_wait, open
    lot: Lot | None = None


def load_taker_tape(base):
    """Match-ID deduplication; use 'crossed' taker leg instead of arbitrary side.

    Archive trades often contain TWO account-side rows per match. Retaining one
    arbitrary row may reverse the aggressor direction; each match must count once.
    """
    folder=Path(base)/"trades"/"PAXG"
    if not folder.is_dir():
        raise FileNotFoundError(f"Missing PAXG public trades: {folder}")
    matches={}
    diagnostics=Counter()
    for file in sorted(folder.glob("*.jsonl.gz")):
        parts=file.name.removesuffix(".jsonl.gz").split("_")
        if len(parts)!=2 or not all(x.isdigit() for x in parts):
            raise ValueError(f"Unexpected archive chunk name: {file.name}")
        start,end=map(int,parts)
        if end<=TEST_START or start>=TEST_END:
            continue
        with gzip.open(file,"rt",encoding="utf8") as fp:
            for line in fp:
                if not line.strip():continue
                r=json.loads(line)
                diagnostics["raw_rows"]+=1
                if r.get("crossed") is not True:
                    diagnostics["non_taker_legs_dropped"]+=1
                    continue
                side=str(r.get("side","")).upper()
                if side not in ("A","B"):
                    diagnostics["invalid_taker_side"]+=1
                    continue
                try:
                    t=ms(r["timestamp"])
                    px=float(r["price"])
                    qty=float(r["size"])
                except (ValueError,TypeError,KeyError):
                    diagnostics["malformed_taker"]+=1
                    continue
                if not (TEST_START<=t<TEST_END) or not(math.isfinite(px) and math.isfinite(qty)
                                                         and px>0 and qty>0):
                    continue
                tid=r.get("trade_id")
                if tid is None:
                    raise ValueError("Cannot deduplicate trade sides without trade_id")
                tid=str(tid)
                print_id=(t,tid)
                if print_id in matches:
                    old=matches[print_id]
                    if old.price!=px or old.qty!=qty or old.taker_side!=side:
                        raise ValueError("Conflicting duplicate trade id: "+tid)
                    diagnostics["taker_deduplicated"]+=1
                    continue
                matches[print_id]=Tape(t,px,qty,side,tid)
                diagnostics["taker_trades"]+=1
    result=sorted(matches.values(),key=lambda t:(t.ts,t.trade_id))
    if not result:raise ValueError("No finalized PAXG taker trades in test window")
    diagnostics["first_utc"]=iso(result[0].ts)
    diagnostics["last_utc"]=iso(result[-1].ts)
    return result,dict(diagnostics)


def maker_fee(price,qty,maker_bps):
    return price*qty*maker_bps/10000.0


def taker_fee(price,qty,taker_bps):
    return price*qty*taker_bps/10000.0


def group_key(side,price):
    return side,round(price,5)


def run_scenario(xau,paxg,tape,zones,qty,tp,maker_bps=0.0,taker_bps=0.0):
    """Chronological single-scenario replay; all six grids share the same book."""
    assert qty>0 and tp in (2.,3.)
    states={f"{side}_{label}":State(side,label,level)
            for side in ("short","long") for label,level in zones[side]}
    groups={}
    counters=Counter()
    closed=[]
    events=[]
    active_x=None
    active_p=None
    max_concurrent=0
    realized_net=0.0
    peak=0.0
    max_dd=0.0
    total_entry_fees=0.0
    total_exit_fees=0.0
    maker_fee_paid_unhedged=0.0
    max_hedge_delay_s=0.0

    # Priority in same-millisecond tie: XAU book, PAXG book, then trade.
    # Post at t cannot fill using a print with ts == t; strict > below.
    timeline=sorted(
        [(b.ts,0,b) for b in xau if TEST_START<=b.ts<TEST_END]+
        [(b.ts,1,b) for b in paxg if TEST_START<=b.ts<TEST_END]+
        [(r.ts,2,r) for r in tape if TEST_START<=r.ts<TEST_END],
        key=lambda row:(row[0],row[1])
    )

    def remove_from_group(gridid):
        # Remove only own remainder; other grids retain queue ahead.
        for key,group in list(groups.items()):
            group.orders=[order for order in group.orders if order.grid_id!=gridid]
            if not group.orders:
                del groups[key]

    def cancel_pending_outside_zone(ref):
        """Use the SAME qty-aware maker-reference spread used for entry zones.

        SHORT: XAU executable SELL - current PAXG best BID < grid threshold.
        LONG:  XAU executable BUY  - current PAXG best ASK > grid threshold.
        At equality the order stays pending. Quote must be fresh before call.
        Filled/hedge_wait/open positions are never cancelled by this rule.
        """
        for state in states.values():
            if state.status!="pending":continue
            value=ref[state.direction]
            if value is None or not price_is_outside(state.direction,value,state.level):
                continue
            gridid=f"{state.direction}_{state.label}"
            remove_from_group(gridid)
            state.status="flat"
            state.armed=False
            counters["unfilled_maker_canceled_spread_reversion"]+=1

    def reserve(used,key,volume):
        used[key]=used.get(key,0.0)+volume

    def execute_close(book_x,book_p,state,used):
        lot=state.lot
        if lot is None or lot.entry_x is None:
            return None
        xa="sell" if lot.direction=="long" else "buy"
        pa="buy" if lot.direction=="long" else "sell"
        xused=used.get(("XAU",xa),0.)
        pused=used.get(("PAXG",pa),0.)
        xp=vwap_slice(book_x,xa,lot.qty,xused)
        pp=vwap_slice(book_p,pa,lot.qty,pused)
        if xp is None or pp is None:return None
        return xp,pp,{("XAU",xa):lot.qty,("PAXG",pa):lot.qty}

    def gross_for_lot(lot,xout,pout):
        if lot.direction=="long":
            return (xout-lot.entry_x)+(lot.entry_p-pout)
        return (lot.entry_x-xout)+(pout-lot.entry_p)

    def mark_liquidation(book_x,book_p):
        used={}
        total=0.0
        n=0
        for state in states.values():
            if state.status!="open":continue
            result=execute_close(book_x,book_p,state,used)
            if result is None:return None
            x,p,res=result
            for k,q in res.items():reserve(used,k,q)
            lot=state.lot
            assert lot is not None
            total+=lot.qty*gross_for_lot(lot,x,p)-lot.entry_fee
            total-=taker_fee(x,lot.qty,taker_bps)+taker_fee(p,lot.qty,taker_bps)
            n+=1
        # Note: unhedged PAXG maker exposure is NOT included in this marked equity.
        if any(state.status=="hedge_wait" for state in states.values()):
            return None
        return total

    for t,kind,obj in timeline:
        if kind==0:
            active_x=obj
            # Evaluate pending spread cancellation on a fresh XAU update too,
            # not just PAXG updates. Never use a stale PAXG book for this.
            if (active_p is not None and
                0 <= t-active_p.ts <= MAX_PAXG_QUOTE_AGE_MS):
                cancel_pending_outside_zone(current_reference((t,active_x,active_p),qty))
            # ONLY hedge AFTER the maker trade timestamp, from a later XAU L2.
            used={}
            for state in states.values():
                if state.status!="hedge_wait":continue
                lot=state.lot
                assert lot is not None
                if t<=lot.maker_fill_ms:continue
                if t-lot.maker_fill_ms > MAX_HEDGE_DELAY_MS:
                    counters["hedge_delay_over_5m"]+=1
                xa="buy" if lot.direction=="long" else "sell"
                xprice=vwap_slice(active_x,xa,lot.qty,used.get(("XAU",xa),0.))
                if xprice is None:
                    counters["hedge_xau_depth_unavailable"]+=1
                    continue
                reserve(used,("XAU",xa),lot.qty)
                lot.entry_x=xprice
                lot.x_hedge_ms=t
                lot.entry_fee=maker_fee(lot.entry_p,lot.qty,maker_bps)+taker_fee(xprice,lot.qty,taker_bps)
                total_entry_fees+=lot.entry_fee
                max_hedge_delay_s=max(max_hedge_delay_s,(t-lot.maker_fill_ms)/1000.)
                state.status="open"
                counters["xau_hedges_executed"]+=1
                if lot.qty < lot.initial_order_qty-1e-9:
                    counters["partially_filled_maker_positions"]+=1
            continue
        if kind==2:
            trade=obj
            action="SELL" if trade.taker_side=="B" else "BUY"
            key=group_key(action,trade.price)
            grp=groups.get(key)
            if grp is None:continue
            # Only *subsequent* trades, no self-confirmation at placement time.
            relevant=[o for o in grp.orders if trade.ts>o.placed_ms]
            if not relevant:continue
            counters["prints_at_active_limit"]+=1
            volume=trade.qty
            consume=min(volume,grp.queue_remaining)
            grp.queue_remaining-=consume
            volume-=consume
            if volume<=1e-10:continue
            for o in list(grp.orders):
                if volume<=1e-10:break
                if trade.ts<=o.placed_ms:continue
                filled=min(o.qty,volume)
                if filled<=1e-10:continue
                volume-=filled
                state=states[o.grid_id]
                if state.status!="pending":
                    raise AssertionError("Maker queue state inconsistent")
                # Cancel remaining unfilled maker shares on FIRST partial/full
                # public-tape match, then hedge matched shares from next XAU book.
                state.lot=Lot(
                    o.grid_id,o.direction,filled,o.limit,trade.ts,o.qty
                )
                state.status="hedge_wait"
                state.armed=False
                grp.orders.remove(o)
                counters["maker_print_matched_order_count"]+=1
                counters["maker_print_matched_volume_milli_oz"]+=round(filled*1000)
            if not grp.orders:
                del groups[key]
            continue

        # PAXG source-book event: use the last *past* XAU source book, not a
        # minute-boundary look-ahead or a forward interpolated snapshot.
        active_p=obj
        if active_x is None or active_x.ts>t or t-active_x.ts>MAX_XAU_QUOTE_AGE_MS:
            counters["paxg_books_without_recent_xau"]+=1
            continue
        ev=(t,active_x,active_p)
        ref=current_reference(ev,qty)
        cancel_pending_outside_zone(ref)
        closing_used={}
        # First close hedged positions. At most one close per grid, and no reentry
        # into an unchanged zone immediately following the TP.
        for state in states.values():
            if state.status!="open":continue
            lot=state.lot
            assert lot is not None
            if lot.x_hedge_ms is None or t<=lot.x_hedge_ms:continue
            close=execute_close(active_x,active_p,state,closing_used)
            if close is None:
                counters["close_depth_insufficient"]+=1
                continue
            xprice,pprice,res=close
            gross_oz=gross_for_lot(lot,xprice,pprice)
            if gross_oz+1e-9<tp:continue
            for k,q in res.items():reserve(closing_used,k,q)
            exit_fee=taker_fee(xprice,lot.qty,taker_bps)+taker_fee(pprice,lot.qty,taker_bps)
            net=lot.qty*gross_oz-lot.entry_fee-exit_fee
            total_exit_fees+=exit_fee
            realized_net+=net
            closed.append({
                "entry_utc":iso(lot.maker_fill_ms),
                "hedge_utc":iso(lot.x_hedge_ms),
                "exit_utc":iso(t),
                "direction":lot.direction,
                "grid":state.label,
                "qty_oz":lot.qty,"initial_order_qty_oz":lot.initial_order_qty,
                "paxg_maker_px":lot.entry_p,"xau_hedge_px":lot.entry_x,
                "xau_exit_px":xprice,"paxg_exit_px":pprice,
                "gross_usd_oz":gross_oz,"gross_usd":lot.qty*gross_oz,
                "entry_fee_usd":lot.entry_fee,"exit_fee_usd":exit_fee,
                "net_usd":net,
                "held_minutes":round((t-lot.x_hedge_ms)/60000.,3),
            })
            state.status="flat"
            state.armed=False
            state.lot=None
        # Pending orders are NOT counted as an open hedge; they consume
        # opportunity until canceled or maker matched.
        for name,state in states.items():
            if state.status!="flat":continue
            value=ref[state.direction]
            if value is None:continue
            if price_is_outside(state.direction,value,state.level):
                state.armed=True
                continue
            if not(state.armed and price_is_inside(state.direction,value,state.level)):
                continue
            sell=state.direction=="long"
            maker_action="SELL" if sell else "BUY"
            level=active_p.ask[0] if sell else active_p.bid[0]
            limit,visible=level
            x_action="buy" if sell else "sell"
            xp=vwap_slice(active_x,x_action,qty,0.)
            if xp is None:
                counters["trigger_with_insufficient_xau_depth"]+=1
                state.armed=False
                continue
            effective=xp-limit
            if ((state.direction=="short" and effective<state.level-1e-9) or
                (state.direction=="long" and effective>state.level+1e-9)):
                counters["maker_entry_quote_no_longer_valid"]+=1
                state.armed=False
                continue
            key=group_key(maker_action,limit)
            if key not in groups:
                groups[key]=Group(maker_action,limit,visible)
            groups[key].orders.append(Order(name,state.direction,qty,limit,t))
            state.status="pending"
            state.armed=False
            counters["maker_orders_posted"]+=1
        max_concurrent=max(max_concurrent,
                           sum(s.status in ("open","hedge_wait") for s in states.values()))
        mtm=mark_liquidation(active_x,active_p)
        if mtm is not None:
            eq=realized_net+mtm
            peak=max(peak,eq)
            max_dd=max(max_dd,peak-eq)
            counters["complete_equity_marks"]+=1
        else:
            counters["incomplete_equity_marks"]+=1

    # Terminal mark: separate risk on unhedged PAXG and hedged pairs.
    terminal=[]
    used={}
    unmarked=0
    if active_x is None or active_p is None:raise ValueError("No 15-day L2")
    for state in states.values():
        if state.status=="pending":
            counters["pending_unfilled_at_terminal"]+=1
            continue
        if state.status=="flat":continue
        lot=state.lot
        assert lot is not None
        if state.status=="open":
            res=execute_close(active_x,active_p,state,used)
            if res is None:
                unmarked+=1
                continue
            xprice,pprice,usage=res
            for k,q in usage.items():reserve(used,k,q)
            exf=taker_fee(xprice,lot.qty,taker_bps)+taker_fee(pprice,lot.qty,taker_bps)
            gross=lot.qty*gross_for_lot(lot,xprice,pprice)
            net=gross-lot.entry_fee-exf
        else:
            # No XAU hedge yet: direct PAXG-only exposure, not imaginary pair.
            pa="buy" if state.direction=="long" else "sell"
            pprice=vwap_slice(active_p,pa,lot.qty,used.get(("PAXG",pa),0.))
            if pprice is None:
                unmarked+=1
                continue
            reserve(used,("PAXG",pa),lot.qty)
            gross=(lot.entry_p-pprice)*lot.qty if state.direction=="long" else (pprice-lot.entry_p)*lot.qty
            exf=taker_fee(pprice,lot.qty,taker_bps)
            net=gross-maker_fee(lot.entry_p,lot.qty,maker_bps)-exf
        terminal.append({
            "direction":state.direction,"grid":state.label,"status":state.status,
            "maker_fill_utc":iso(lot.maker_fill_ms),
            "qty_oz":lot.qty,"gross_usd":gross,"hypothetical_liquidation_net_usd":net,
        })
    terminal_total=sum(p["hypothetical_liquidation_net_usd"] for p in terminal)
    count_pending=sum(s.status=="pending" for s in states.values())
    count_unhedged=sum(s.status=="hedge_wait" for s in states.values())
    count_hedged=sum(s.status=="open" for s in states.values())
    result={
        "qty_per_grid_oz":qty,"tp_gross_usd_per_oz":tp,
        "maker_bps":maker_bps,"taker_bps":taker_bps,
        "paxg_maker_orders_posted":counters["maker_orders_posted"],
        "maker_orders_with_any_public_trade_allocated":counters["maker_print_matched_order_count"],
        "maker_matched_public_volume_oz":round(counters["maker_print_matched_volume_milli_oz"]/1000.,6),
        "partial_maker_fills_hedged":counters["partially_filled_maker_positions"],
        "xau_hedges_executed":counters["xau_hedges_executed"],
        "tp_closed_positions":len(closed),
        "closed_long":sum(t["direction"]=="long" for t in closed),
        "closed_short":sum(t["direction"]=="short" for t in closed),
        "closed_gross_usd":round(sum(t["gross_usd"] for t in closed),6),
        "closed_net_usd":round(realized_net,6),
        "paid_entry_fee_usd":round(total_entry_fees,6),
        "paid_exit_fee_usd":round(total_exit_fees,6),
        "terminal_pending_unfilled":count_pending,
        "terminal_hedge_wait_unhedged_paxg":count_unhedged,
        "terminal_open_hedged_grids":count_hedged,
        "terminal_marked_positions":len(terminal),
        "terminal_unmarked_positions":unmarked,
        "terminal_liquidation_net_usd":round(terminal_total,6) if not unmarked else None,
        "total_net_with_terminal_liquidation_usd":round(realized_net+terminal_total,6) if not unmarked else None,
        "max_complete_mark_drawdown_usd":round(max_dd,6),
        "complete_equity_mark_samples":counters["complete_equity_marks"],
        "incomplete_equity_mark_samples":counters["incomplete_equity_marks"],
        "max_maker_fill_to_xau_hedge_delay_s":round(max_hedge_delay_s,3),
        "maker_orders_canceled_on_spread_reversion":counters["unfilled_maker_canceled_spread_reversion"],
        "paxg_snapshot_no_fresh_xau":counters["paxg_books_without_recent_xau"],
        "maker_limit_exact_trade_prints":counters["prints_at_active_limit"],
        "max_concurrent_nonflat_positions":max_concurrent,
    }
    return result,closed,terminal


def write_csv(path,rows,default_fields):
    cols=list(rows[0]) if rows else default_fields
    with Path(path).open("w",newline="",encoding="utf8") as f:
        wr=csv.DictWriter(f,fieldnames=cols)
        wr.writeheader()
        if rows:wr.writerows(rows)


def run(data,zones_path,output):
    data=Path(data)
    out=Path(output)
    out.mkdir(parents=True,exist_ok=True)
    x_times,x_books_all=read_books(data,"XAU",TEST_START,TEST_END-1)
    p_times,p_books_all=read_books(data,"PAXG",TEST_START,TEST_END-1)
    # read_books deliberately includes up to 3 minutes of pre-window history.
    # Preserve it as as-of context ONLY, never allow old snapshots to POST orders.
    x_books=[b for b in x_books_all if TEST_START<=b.ts<TEST_END]
    p_books=[b for b in p_books_all if TEST_START<=b.ts<TEST_END]
    tape,trade_stats=load_taker_tape(data)
    rows=[]
    all_closed=[]
    all_terminal=[]
    for quantity in (0.5,1.0):
        zones=load_frozen_zones(zones_path,quantity)
        print("FROZEN_ZONES",quantity,zones,flush=True)
        for tp in (2.0,3.0):
            for fee_label,(m_bps,t_bps) in FEE_PROFILES.items():
                summary,closed,terminal=run_scenario(
                    x_books,p_books,tape,zones,quantity,tp,m_bps,t_bps
                )
                summary["fee_profile"]=fee_label
                rows.append(summary)
                all_closed.extend([{"fee_profile":fee_label,**summary_fields,**r}
                                   for r in closed
                                   for summary_fields in [{"qty_per_grid_oz":quantity,
                                                           "tp_gross_usd_per_oz":tp}]])
                all_terminal.extend([{"fee_profile":fee_label,"qty_per_grid_oz":quantity,
                                      "tp_gross_usd_per_oz":tp,**r} for r in terminal])
                print("CAUSAL_SCENARIO",quantity,tp,fee_label,
                      "maker_posted",summary["paxg_maker_orders_posted"],
                      "maker_matched",summary["maker_orders_with_any_public_trade_allocated"],
                      "tp_closed",summary["tp_closed_positions"],
                      "total_marked",summary["total_net_with_terminal_liquidation_usd"],
                      flush=True)
    write_csv(out/"causal_tp2_tp3_scenarios.csv",rows,
              ["qty_per_grid_oz","tp_gross_usd_per_oz","fee_profile"])
    write_csv(out/"causal_closed_trades.csv",all_closed,
              ["fee_profile","qty_per_grid_oz","tp_gross_usd_per_oz",
               "entry_utc","hedge_utc","exit_utc","net_usd"])
    write_csv(out/"causal_terminal_positions.csv",all_terminal,
              ["fee_profile","qty_per_grid_oz","tp_gross_usd_per_oz","status","hypothetical_liquidation_net_usd"])
    report={
        "experiment":"HISTORICAL_REPLAY_AFTER_FORWARD_DATA_INSPECTED__NOT_FRESH_OOS",
        "frozen_calibration":"12d daily-quantile grid, no new optimization",
        "date_start_utc":iso(TEST_START),"date_end_exclusive_utc":iso(TEST_END),
        "execution":{
            "maker_order_time_to_live_seconds":None,
            "maker_pending_cancel_rule":"SHORT when executable short maker-reference spread < frozen grid level; LONG when executable long maker-reference spread > frozen grid level; require fresh L2. Never cancel solely because 5 minutes elapsed.",
            "max_xau_quote_age_on_entry_exit_seconds":MAX_XAU_QUOTE_AGE_MS/1000,
            "paxg_maker_limit":"current PAXG best bid for BUY; best ask for SELL",
            "pending_queue":"visible best-level quantity ahead + FIFO across our grids at same price",
            "public_prints":"deduplicated by match trade_id; only crossed=true TAKER leg; require exact maker price and opposite aggressor action",
            "partials":"first trade-allocated quantity cancels maker remainder, hedges allocated quantity only at next XAU L2 snapshot",
            "hedge":"next strictly later XAU snapshot after maker partial/full trade; reserve L2 20-level depth across grids",
            "exit":"gross executable TP 2/3 USD/oz; all TP exits use 2 taker legs",
            "no_position_stop_or_timeout":True,
        },
        "data":{
            "xau_book_count":len(x_books),"paxg_book_count":len(p_books),
            "taker_prints":len(tape),"trade_diagnostics":trade_stats,
        },
        "caveats":[
            "This is a DEVELOPER replay on the already-inspected 15-day set, NOT an untouched forward test.",
            "No actual maker order IDs, queue position, cancellations, hidden liquidity or network latency; tape-based fills still HYPOTHETICAL.",
            "Conservative assumption: visible book quantity must trade through before our order, without estimating queue cancellation.",
            "Partial maker fill exposure modeled by exact matched volume; assumed immediate cancel remainder (cannot verify cancellation latency).",
            "XAU hedge only at a strictly later XAU L2 snapshot; no lookahead for maker-to-hedge pricing.",
            "Uses two L2 snapshots to price instant exits, which are still unsynchronized by <=20 seconds; all L2 market impact not reproduced.",
            "No margin, liquidation funding cashflows, account-level cross-margin netting or minimum trade quantity modeled.",
            "No stop loss. TP winners alone are not meaningful without floating losses and PAXG unhedged exposure.",
            "Min order size for partial PAXG/XAU hedge must be checked before claiming executable partial fills.",
            "Public aggressive trade records were selected from crossed=true account leg and match ID deduplicated; absent prints may reflect incomplete archive coverage.",
            "Different size, TP and fee scenarios represent independent hypothetical accounts; never sum their results.",
        ],
        "scenarios":rows,
    }
    (out/"causal_maker_first_summary.json").write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print("CAUSAL_REPORT",out/"causal_maker_first_summary.json",flush=True)
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data",required=True)
    ap.add_argument("--zones",required=True)
    ap.add_argument("--output",required=True)
    args=ap.parse_args()
    run(args.data,args.zones,args.output)


if __name__=="__main__":
    main()
