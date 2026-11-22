#!/usr/bin/env python3
"""Locked 12-day calibrated, next-15-day two-sided XAU/PAXG multi-grid replay.

Research only. Historical 1-minute L2 snapshots do NOT prove maker fills,
queue priority, intraminute path, order latency, or funding cash flows.
Each TP is $2 or $3 of executable GROSS spread improvement per oz; fees
are subtracted separately from realized and terminal-marked P&L.
"""
import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from scripts.analyze_lighter_spread_feasibility import (
    iso, ms, read_books, paired, executable,
)

TRAIN_START = ms("2026-10-23T01:00:00Z")
TEST_START = ms("2026-11-04T01:00:00Z")
TEST_END = ms("2026-11-19T00:00:00Z")  # exclusive, final trade watermark
FEE_PROFILES = {
    "standard": (0.0, 0.0),   # maker bps, taker bps
    "plus": (0.5, 0.5),
    "premium": (0.4, 2.8),
}
DIRECTIONS = ("short", "long")


@dataclass
class Position:
    direction: str
    grid: str
    level: float
    start_ms: int
    entry_x: float
    entry_p: float
    entry_fee_usd: float
    entry_gross_spread: float


@dataclass
class Grid:
    direction: str
    name: str
    level: float
    armed: bool = False
    position: Position | None = None


def load_frozen_zones(path, qty):
    source = json.loads(Path(path).read_text(encoding="utf8"))
    if (source.get("days") != 12 or
        ms(source["calibration_start_inclusive_utc"]) != TRAIN_START or
        ms(source["calibration_end_exclusive_utc"]) != TEST_START or
        ms(source["reserved_test_start_inclusive_utc"]) != TEST_START or
        ms(source["reserved_test_end_exclusive_utc"]) != TEST_END):
        raise ValueError("Zone report has wrong training/forward-test boundary")
    data = source["equal_day_weight_zones_by_qty"][str(float(qty))]["zones_usd_per_oz"]
    chosen = {
        "short": [
            ("P75", data["short_maker"]["mean_of_12_daily_P75"]),
            ("P90", data["short_maker"]["mean_of_12_daily_P90"]),
            ("P95", data["short_maker"]["mean_of_12_daily_P95"]),
        ],
        "long": [
            ("P25", data["long_maker"]["mean_of_12_daily_P25"]),
            ("P10", data["long_maker"]["mean_of_12_daily_P10"]),
            ("P5", data["long_maker"]["mean_of_12_daily_P5"]),
        ],
    }
    for name,levels in chosen.items():
        for label,value in levels:
            if not isinstance(value,(float,int)) or not math.isfinite(value):
                raise ValueError(f"Invalid {name} {label} calibration level")
    if not (chosen["short"][0][1]<chosen["short"][1][1]<chosen["short"][2][1]):
        raise ValueError("Short grid order invalid")
    if not (chosen["long"][0][1]>chosen["long"][1][1]>chosen["long"][2][1]):
        raise ValueError("Long grid order invalid")
    return chosen


def vwap_slice(book, action, quantity, previously_used):
    """Marginal executable price from remaining depth; independent orders share depth."""
    levels = book.ask if action == "buy" else book.bid
    start = previously_used
    end = start + quantity
    covered = 0.0
    gross = 0.0
    for price, size in levels:
        begin = max(start, covered)
        stop = min(end, covered + size)
        if stop > begin:
            gross += (stop - begin) * price
        covered += size
        if covered + 1e-9 >= end:
            return gross / quantity
    return None


def execution(event, direction, quantity, used, mode, opening):
    """Return (x_price,p_price,fee_role_x,fee_role_p,reservations) or None.

    PAXG passive maker entry is an optimistic fill at the displayed quote.
    XAU is always taker; both legs are taker on exit. Depth is shared across
    simultaneous orders on each venue/side within one minute snapshot.
    """
    _, x, p = event
    if direction == "short":
        xa, pa = ("sell","buy") if opening else ("buy","sell")
    else:
        xa, pa = ("buy","sell") if opening else ("sell","buy")

    xused = used.get(("XAU",xa),0.0)
    xp = vwap_slice(x, xa, quantity, xused)
    maker = opening and mode == "maker_assumed"
    if maker:
        pp = p.bid[0][0] if pa == "buy" else p.ask[0][0]
    else:
        pused = used.get(("PAXG",pa),0.0)
        pp = vwap_slice(p, pa, quantity, pused)
    if xp is None or pp is None:
        return None
    reservations = {("XAU",xa):quantity}
    if not maker:
        reservations[("PAXG",pa)] = quantity
    return xp,pp,"taker",("maker" if maker else "taker"),reservations


def reserve(used, reservations):
    for name, quantity in reservations.items():
        used[name] = used.get(name,0.0) + quantity


def commission_usd(xprice,pprice,quantity,xrole,prole,maker_bps,taker_bps):
    xrate = maker_bps if xrole=="maker" else taker_bps
    prate = maker_bps if prole=="maker" else taker_bps
    return quantity * (xprice * xrate + pprice * prate) / 10000


def gross_oz(position, exit_x, exit_p):
    if position.direction == "short":
        return (position.entry_x - exit_x) + (exit_p - position.entry_p)
    return (exit_x - position.entry_x) + (position.entry_p - exit_p)


def current_reference(event, quantity):
    """Use frozen 12-day maker-based signal definitions for both scenarios."""
    _, x, p = event
    r=executable(event,quantity)
    short = r["maker_signal"]
    long = r["x_buy"] - p.ask[0][0] if r["x_buy"] is not None else None
    return {"short":short,"long":long}


def price_is_outside(direction, ref, threshold):
    return ref < threshold if direction=="short" else ref > threshold


def price_is_inside(direction, ref, threshold):
    return ref >= threshold if direction=="short" else ref <= threshold


def run_scenario(events,zones,qty,tp,mode,fee_name, maker_bps,taker_bps):
    grids = [Grid(direction,name,level) for direction in DIRECTIONS
             for name,level in zones[direction]]
    realized = []
    open_count=0
    skipped_depth=0
    rejected_bad_maker_spread=0
    equity_peak=0.0
    max_dd=0.0
    obs_equity=0.0
    max_concurrent=0
    max_closed_hold_minutes=0.0
    fees_paid=0.0
    realized_running=0.0
    last_event=None
    for event in events:
        last_event=event
        t=event[0]
        reference=current_reference(event,qty)
        used={}
        # Close independently; only when actual executable gross P&L >= TP per oz.
        # No stop-loss / timeout: positions that do not reach TP remain exposed.
        for grid in grids:
            position=grid.position
            if position is None:continue
            prices=execution(event,grid.direction,qty,used,mode,opening=False)
            if prices is None:
                skipped_depth+=1
                continue
            x,p,xrole,prole,reservations=prices
            gain=gross_oz(position,x,p)
            if gain+1e-9 < tp:continue
            exit_fee=commission_usd(x,p,qty,xrole,prole,maker_bps,taker_bps)
            net=qty*gain-position.entry_fee_usd-exit_fee
            held=(t-position.start_ms)/60000
            realized.append({
                "entry_utc":iso(position.start_ms),"exit_utc":iso(t),
                "direction":grid.direction,"grid":grid.name,"zone_usd_oz":grid.level,
                "quantity_oz":qty,"tp_gross_usd_oz":tp,"mode":mode,"fee_profile":fee_name,
                "entry_x":position.entry_x,"entry_p":position.entry_p,
                "exit_x":x,"exit_p":p,"gross_usd_oz":gain,
                "gross_usd":qty*gain,
                "entry_fee_usd":position.entry_fee_usd,
                "exit_fee_usd":exit_fee,"net_usd":net,
                "held_minutes":held,"status":"TP",
            })
            fees_paid+=exit_fee
            realized_running+=net
            max_closed_hold_minutes=max(max_closed_hold_minutes,held)
            reserve(used,reservations)
            grid.position=None
            # Never re-open immediately inside the same zone after exit.
            grid.armed=False

        # Arm only after price is observed OUTSIDE that grid's trigger zone.
        # One concurrent position per grid; opposite grids remain independent.
        for grid in grids:
            if grid.position is not None:continue
            ref=reference[grid.direction]
            if ref is None:continue
            if price_is_outside(grid.direction,ref,grid.level):
                grid.armed=True
                continue
            if not(grid.armed and price_is_inside(grid.direction,ref,grid.level)):
                continue
            entry=execution(event,grid.direction,qty,used,mode,opening=True)
            if entry is None:
                skipped_depth+=1
                grid.armed=False
                continue
            x,p,xrole,prole,reservations=entry
            effective_spread=x-p
            # Pre-fill maker cancel if XAU executable VWAP has deteriorated.
            if mode=="maker_assumed" and (
                (grid.direction=="short" and effective_spread < grid.level-1e-8) or
                (grid.direction=="long" and effective_spread > grid.level+1e-8)
            ):
                rejected_bad_maker_spread+=1
                grid.armed=False
                continue
            entry_fee=commission_usd(x,p,qty,xrole,prole,maker_bps,taker_bps)
            grid.position=Position(grid.direction,grid.name,grid.level,t,x,p,
                                   entry_fee,effective_spread)
            fees_paid+=entry_fee
            reserve(used,reservations)
            grid.armed=False
            open_count+=1

        max_concurrent=max(max_concurrent,sum(g.position is not None for g in grids))
        # Floating equity marked with executable exit prices (not theoretical mid).
        marks=0.0
        marked_positions=0
        virtual_used={}
        for grid in grids:
            position=grid.position
            if position is None:continue
            prices=execution(event,grid.direction,qty,virtual_used,mode,opening=False)
            if prices is None:continue
            x,p,xrole,prole,res=prices
            reserve(virtual_used,res)
            exit_fee=commission_usd(x,p,qty,xrole,prole,maker_bps,taker_bps)
            marks+=qty*gross_oz(position,x,p)-position.entry_fee_usd-exit_fee
            marked_positions+=1
        realized_total=realized_running
        # Not all positions may be markable (depth); never claim fully marked.
        if marked_positions==sum(g.position is not None for g in grids):
            obs_equity=realized_total+marks
            equity_peak=max(equity_peak,obs_equity)
            max_dd=max(max_dd,equity_peak-obs_equity)

    if last_event is None:raise ValueError("Empty forward test period")
    terminal_marks=[]
    terminal_used={}
    unmarked=0
    for grid in grids:
        pos=grid.position
        if pos is None:continue
        final=execution(last_event,grid.direction,qty,terminal_used,mode,opening=False)
        if final is None:
            unmarked+=1
            continue
        x,p,xrole,prole,r=final
        reserve(terminal_used,r)
        fee=commission_usd(x,p,qty,xrole,prole,maker_bps,taker_bps)
        gross=qty*gross_oz(pos,x,p)
        terminal_marks.append({
            "direction":grid.direction,"grid":grid.name,
            "entry_utc":iso(pos.start_ms),"marked_at_utc":iso(last_event[0]),
            "zone_usd_oz":grid.level,"gross_usd":gross,"entry_fee_usd":pos.entry_fee_usd,
            "hypothetical_exit_fee_usd":fee,
            "mark_to_liquidation_net_usd":gross-pos.entry_fee_usd-fee,
            "held_minutes":(last_event[0]-pos.start_ms)/60000,
        })
    rnet=realized_running
    unet=sum(r["mark_to_liquidation_net_usd"] for r in terminal_marks)
    result={
        "qty_oz_per_grid":qty,"tp_gross_usd_per_oz":tp,
        "execution_mode":mode,"fee_profile":fee_name,
        "maker_fee_bps":maker_bps,"taker_fee_bps":taker_bps,
        "opens":open_count,"tp_closed_trades":len(realized),
        "tp_long_count":sum(r["direction"]=="long" for r in realized),
        "tp_short_count":sum(r["direction"]=="short" for r in realized),
        "winning_net_trades":sum(r["net_usd"]>0 for r in realized),
        "realized_win_rate_pct":round(100*sum(r["net_usd"]>0 for r in realized)/len(realized),3) if realized else None,
        "realized_gross_usd":round(sum(r["gross_usd"] for r in realized),6),
        "realized_net_usd":round(rnet,6),
        "gross_fees_paid_usd":round(fees_paid,6),
        "unclosed_grids":sum(g.position is not None for g in grids),
        "terminal_marked_grids":len(terminal_marks),
        "terminal_unmarked_grids":unmarked,
        "terminal_unrealized_net_usd":round(unet,6) if not unmarked else None,
        "total_net_including_terminal_mark_usd":round(rnet+unet,6) if not unmarked else None,
        "max_marked_equity_drawdown_usd":round(max_dd,6),
        "max_concurrent_grids":max_concurrent,
        "max_closed_hold_minutes":round(max_closed_hold_minutes,2),
        "depth_blocked_events":skipped_depth,
        "maker_pre_fill_cancel_checks_failed":rejected_bad_maker_spread,
        "marking_note":"Terminal positions are not actual TP closes; hypothetical executable liquidation only",
    }
    return result, realized, terminal_marks


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data",required=True)
    parser.add_argument("--zones",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--quantities",type=float,nargs="+",default=[0.5,1.0])
    parser.add_argument("--tp",type=float,nargs="+",default=[2.0,3.0])
    parser.add_argument("--max-quote-age-s",type=int,default=120)
    parser.add_argument("--max-snapshot-skew-s",type=int,default=120)
    args=parser.parse_args(argv)
    if sorted(args.tp)!=[2.0,3.0]:
        parser.error("TP experiment is frozen to [2,3] gross USD/oz; do not optimize from holdout")
    if min(args.quantities)<=0 or args.max_quote_age_s<=0 or args.max_snapshot_skew_s<0:
        parser.error("Invalid positive quantities / quote ages")
    xau=read_books(Path(args.data),"XAU",TEST_START,TEST_END-1)
    paxg=read_books(Path(args.data),"PAXG",TEST_START,TEST_END-1)
    all_events=paired(xau,paxg,TEST_START,TEST_END-1,1,args.max_quote_age_s*1000)
    events=[event for event in all_events
            if abs(event[1].ts-event[2].ts)<=args.max_snapshot_skew_s*1000]
    if not events:raise ValueError("No sufficiently synchronized test snapshots")
    output=Path(args.output)
    output.mkdir(parents=True,exist_ok=True)
    rows,all_trades,all_terminal=[],[],[]
    for qty in args.quantities:
        zones=load_frozen_zones(args.zones,qty)
        print("FROZEN_ZONES",qty,zones,flush=True)
        for tp in args.tp:
            for mode in ("maker_assumed","taker"):
                for fee_name,(maker_bps,taker_bps) in FEE_PROFILES.items():
                    result,trades,terminal=run_scenario(
                        events,zones,qty,tp,mode,fee_name,maker_bps,taker_bps
                    )
                    rows.append(result)
                    all_trades.extend(trades)
                    for mark in terminal:
                        all_terminal.append({
                            "qty_oz_per_grid":qty,"tp_gross_usd_per_oz":tp,
                            "mode":mode,"fee_profile":fee_name,**mark,
                        })
                    print("SCENARIO",qty,tp,mode,fee_name,
                          "tp_closes",result["tp_closed_trades"],
                          "realized_net",result["realized_net_usd"],
                          "open",result["unclosed_grids"],
                          "total_marked_net",result["total_net_including_terminal_mark_usd"],
                          flush=True)
    def write_csv(path,records):
        if not records:return
        with (output/path).open("w",newline="",encoding="utf8") as f:
            writer=csv.DictWriter(f,fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    write_csv("tp_2_vs_3_scenarios.csv",rows)
    write_csv("trades_all_scenarios.csv",all_trades)
    write_csv("terminal_open_positions.csv",all_terminal)
    coverage={
        "test_start_utc":iso(TEST_START),"test_end_exclusive_utc":iso(TEST_END),
        "first_paired_event_utc":iso(events[0][0]),
        "last_paired_event_utc":iso(events[-1][0]),
        "possible_minute_ticks":(TEST_END-TEST_START)//60000,
        "paired_snapshot_ticks_before_skew_filter":len(all_events),
        "paired_snapshot_ticks":len(events),
        "max_snapshot_skew_seconds":args.max_snapshot_skew_s,
        "quote_max_age_seconds":args.max_quote_age_s,
        "scenario_count":len(rows),
        "tp_values_gross_usd_per_oz":args.tp,
        "fees_basis_points_maker_taker":FEE_PROFILES,
        "scenario_results":rows,
        "caveats":[
            "Historical 1-minute snapshots do not prove maker queue fills; maker_assumed is an optimistic counterfactual.",
            "Both-taker mode is a separate execution sensitivity, not a verified historical trade log.",
            "TP=2 and 3 is GROSS executable spread gain per oz before commissions; reported net includes all four trade fees.",
            "Floating P&L including terminal liquidation must be considered; realized profits alone are misleading.",
            "No stop loss or timeout by design; one open position per grid and rearm only after exit and leaving zone.",
            "Each TP scenario is a separate independent portfolio. Do NOT add different TP, fee or mode scenarios.",
            "Minute as-of snapshots use timestamps <= decision minute only; no interpolation across missing data.",
            "Both legs are per-ounce units; account-level portfolio netting, funding, maintenance margin and liquidation are not implemented.",
            "Maker fees on entry only (1 maker + 3 takers); fees charged individually on XAU and PAXG open and close.",
            "Additional latency/adverse selection and potential API order limits not modeled.",
            "Day-12 frozen mean daily quantile zones were selected before reading forward-test data.",
        ],
    }
    (output/"forward_15d_summary.json").write_text(json.dumps(coverage,indent=2,ensure_ascii=False))
    print("REPORT",output/"forward_15d_summary.json",flush=True)
    return coverage


if __name__=="__main__":
    main()
