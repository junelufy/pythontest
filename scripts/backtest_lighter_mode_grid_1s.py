#!/usr/bin/env python3
"""Offline Lighter XAU/PAXG mode-centered grid historical paper replay.

1) Calibrate from FIRST 7 DAYS ONLY: a 1-minute sampled MID/MID spread
   histogram; modal *bin midpoint* is center (not mean/SD/percentile).
2) Place grid thresholds center +/- k*step; extend only on observed test prices.
3) Short >= grid -> PAXG BUY maker at bid; short < grid -> cancel pending.
   Long <= grid -> PAXG SELL maker at ask; long > grid -> cancel pending.
   Repost on next qualifying quote; no 5-minute maker TTL.
4) Public PAXG taker prints model conditional maker fills/queue; XAU TAKER
   hedges at FIRST XAU source book >= maker_fill_timestamp + 2000ms.
5) Close each independent hedged grid when GROSS spread P&L >= TP per oz.

HISTORICAL DEVELOPMENT REPLAY, NOT order-ack/fill proof or fresh OOS data.
No live orders, network access, funding/margin assumptions or data downloads.
"""
import argparse
import csv
import heapq
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from scripts.analyze_lighter_spread_feasibility import iso, ms
from scripts.forwardtest_lighter_spread_grid_15d import FEE_PROFILES, vwap_slice
from scripts.replay_lighter_causal_maker_first_15d import load_taker_tape
from scripts.replay_lighter_1s_frozen_tp2_tp3 import (
    FIRST, END, InvalidBook, get_manifest, iter_books, merge_timeline,
)

TRAIN_BEGIN = ms(FIRST)
TRAIN_END = TRAIN_BEGIN + 7 * 86400 * 1000
TEST_END = ms(END)
HEDGE_LATENCY_MS = 2000
EPS = 1e-8


def mid_spread(x, p):
    return (x.bid[0][0] + x.ask[0][0] - p.bid[0][0] - p.ask[0][0]) / 2.0


def spread_refs(x, p, qty):
    """Conservative executable direction refs: XAU taker / PAXG maker limit."""
    xs = vwap_slice(x, "sell", qty, 0.)
    xb = vwap_slice(x, "buy", qty, 0.)
    return {"short": xs - p.bid[0][0] if xs is not None else None,
            "long": xb - p.ask[0][0] if xb is not None else None}


def executable(book, side, qty, used):
    k = (side,)
    price = vwap_slice(book, side, qty, used.get(k, 0.))
    if price is not None:
        used[k] = used.get(k, 0.) + qty
    return price


def modal_histogram_center(samples, bin_width):
    """Non-peeking, frequency-based center. No mean, SD or quantile grids."""
    if not samples or bin_width <= 0 or not math.isfinite(bin_width):
        raise ValueError("Need valid nonempty spread samples and positive bin width")
    counts = Counter(math.floor(v / bin_width) for v in samples)
    peak = max(counts.values())
    # A tie uses median only to resolve equal peak counts, NOT as grid center.
    med = statistics.median(samples)
    candidates = (k for k, v in counts.items() if v == peak)
    winner = min(candidates, key=lambda k: (abs((k + .5) * bin_width - med), k))
    return (winner + .5) * bin_width, winner, peak


def calibration_distribution(source, records, bin_width, max_age_ms):
    """Never even open a chunk beginning at/after TRAIN_END."""
    records7 = {s: [r for r in records[s] if ms(r["start"]) < TRAIN_END]
                for s in ("XAU", "PAXG")}
    counters = Counter()
    def stream(sym, priority):
        for obj in iter_books(source, sym, records7[sym], counters):
            yield (obj.ts, priority, None if isinstance(obj, InvalidBook) else obj)
    ordered = heapq.merge(stream("XAU", 0), stream("PAXG", 1),
                          key=lambda e: (e[0], e[1]))
    x = p = None
    last_sample_minute = None
    samples = []
    bins = Counter()
    for t, kind, book in ordered:
        if not (TRAIN_BEGIN <= t < TRAIN_END):
            continue
        if kind == 0:
            x = book
        else:
            p = book
        if (kind != 1 or p is None or x is None or
                t - x.ts > max_age_ms or t - p.ts > max_age_ms):
            continue
        minute = t // 60000
        if minute == last_sample_minute:
            continue
        value = mid_spread(x, p)
        samples.append(value)
        bins[math.floor(value / bin_width)] += 1
        last_sample_minute = minute
    if len(samples) < 7 * 1440 * .75:
        raise ValueError(f"Too few valid 7d one-minute observations: {len(samples)}")
    center, winner, highest = modal_histogram_center(samples, bin_width)
    histogram = [{"lower_usd_oz": k * bin_width,
                  "upper_usd_oz": (k + 1) * bin_width,
                  "minutes": count, "fraction": count / len(samples),
                  "modal_bin": k == winner}
                 for k, count in sorted(bins.items())]
    result = {"train_start_utc": iso(TRAIN_BEGIN), "train_end_exclusive_utc": iso(TRAIN_END),
              "sample_method": "first fresh paired PAXG 1s book per minute, XAU/PAXG mid-mid",
              "bin_width_usd_oz": bin_width,
              "center_method": "midpoint of the most-frequent fixed-width histogram bin",
              "tie_rule": "candidate center closest to sample median, then lowest bin",
              "center_usd_oz": center, "spread_min_usd_oz": min(samples),
              "spread_max_usd_oz": max(samples), "n_sampled_minutes": len(samples),
              "modal_bin_minutes": highest, "modal_bin_fraction": highest / len(samples),
              "raw_xau_rows": counters["XAU_rows_seen"],
              "raw_paxg_rows": counters["PAXG_rows_seen"],
              "invalid_xau_books": counters["XAU_rejected"],
              "invalid_paxg_books": counters["PAXG_rejected"]}
    return result, histogram


def group_key(side, px):
    return (side, round(px, 6))


def crossed_by_trade(action, limit, trade):
    if action == "BUY":
        return trade.taker_side == "A" and trade.price <= limit + EPS
    if action == "SELL":
        return trade.taker_side == "B" and trade.price >= limit - EPS
    raise ValueError(action)


@dataclass
class Pending:
    key: str
    side: str
    limit: float
    qty: float
    posted_ms: int


@dataclass
class Group:
    side: str
    limit: float
    ahead: float
    orders: list = field(default_factory=list)


@dataclass
class Lot:
    qty: float
    original_qty: float
    paxg_px: float
    fill_ms: int
    fill_reason: str
    xau_px: float | None = None
    hedge_ms: int | None = None


@dataclass
class Grid:
    side: str
    number: int
    level: float
    status: str = "flat"  # flat,pending,hedge_wait,open
    lot: Lot | None = None
    reset_after_tp: bool = False

    @property
    def key(self):
        return f"{self.side}_{self.number}"


def zones_for_observed(center, step, references, grids, max_levels):
    """Build further levels only when a live (non-future) quote reaches them."""
    for direction in ("short", "long"):
        value = references[direction]
        if value is None:
            continue
        distance = (value - center) if direction == "short" else (center - value)
        if distance < step - EPS:
            continue
        count = max(0, math.floor((distance + EPS) / step))
        if count > max_levels:
            raise RuntimeError(f"Grid {direction} needs {count} levels > max_levels={max_levels}; "
                               "do not silently clip risk, choose explicit higher cap")
        for n in range(1, count + 1):
            key = f"{direction}_{n}"
            if key not in grids:
                price = center + n * step if direction == "short" else center - n * step
                grids[key] = Grid(direction, n, round(price, 8))


def qualifies(side, spread, level):
    return spread >= level - EPS if side == "short" else spread <= level + EPS


def leave_zone(side, spread, level):
    return spread < level - EPS if side == "short" else spread > level + EPS


def pnl_per_oz(side, p_entry, x_entry, p_exit, x_exit):
    if side == "short":
        return (x_entry - x_exit) + (p_exit - p_entry)
    return (x_exit - x_entry) + (p_entry - p_exit)


def fee_reprice(row, maker_bps, taker_bps):
    """Return fee-inclusive P&L from the SAME gross execution path."""
    q = row["qty_oz"]
    maker_in = q * row["paxg_entry_px"] * maker_bps / 1e4
    x_in = row.get("xau_entry_px")
    x_out = row.get("xau_exit_px")
    p_out = row.get("paxg_exit_px")
    if p_out is None:
        return None
    if x_in is None:
        # Unhedged PAXG-only risk in terminal marker.
        cost = maker_in + q * p_out * taker_bps / 1e4
    elif x_out is None:
        return None
    else:
        cost = maker_in + q * (x_in + x_out + p_out) * taker_bps / 1e4
    return round(row["gross_usd"] - cost, 6), round(cost, 6)


def replay(timeline, center, step, qty, tp, max_age_ms=3000,
           max_levels=64, record_events=False, test_start=TRAIN_END,
           test_end=TEST_END, hedge_latency_ms=HEDGE_LATENCY_MS,
           snapshot_dd_every_ms=60000):
    """Causal paper simulation. No time-based maker cancel and no anticipatory hedge."""
    grids = {}
    groups = {}
    stats = Counter()
    open_books = {"XAU": None, "PAXG": None}
    closed, events = [], []
    last_valid_mark = None
    peak, drawdown, last_drawdown_sample = 0., 0., None
    realized_gross = 0.
    last_ts = None
    max_nonflat = 0
    max_exposure_oz = 0.

    def log(kind, t, grid=None, **extra):
        if record_events:
            events.append({"event": kind, "time_utc": iso(t),
                           "grid": grid.key if grid else "",
                           "grid_level": grid.level if grid else None,
                           **extra})

    def remove_order(key):
        for gkey, group in list(groups.items()):
            group.orders = [o for o in group.orders if o.key != key]
            if not group.orders:
                del groups[gkey]

    def register_fill(order, amount, t, reason):
        grid = grids[order.key]
        if grid.status != "pending" or amount <= 0:
            raise AssertionError("Unexpected maker fill state")
        grid.lot = Lot(amount, order.qty, order.limit, t, reason)
        grid.status = "hedge_wait"
        remove_order(order.key)  # any unfilled remainder cancelled on partial
        stats["maker_paper_fills"] += 1
        stats["maker_paper_filled_milli_oz"] += round(amount * 1000)
        stats["through_fills" if reason == "through" else "exact_fills"] += 1
        log("MAKER_PAPER_FILL", t, grid, maker_limit=order.limit,
            fill_qty_oz=amount, public_trade_type=reason,
            hedge_target_utc=iso(t + hedge_latency_ms))

    def on_trade(t, trade):
        action = "BUY" if trade.taker_side == "A" else "SELL"
        for group in list(groups.values()):
            if group.side != action or not crossed_by_trade(group.side, group.limit, trade):
                continue
            eligible = [o for o in list(group.orders) if o.posted_ms < t]
            if not eligible:
                continue
            exact = abs(trade.price - group.limit) <= EPS
            stats["eligible_public_prints"] += 1
            if not exact:
                # Conditional price-through hypothesis, not proven order fills.
                for order in eligible:
                    register_fill(order, order.qty, t, "through")
                continue
            stats["exact_public_prints"] += 1
            available = trade.qty
            consumed = min(available, group.ahead)
            group.ahead -= consumed
            available -= consumed
            if available <= EPS:
                continue
            for order in eligible:
                if available <= EPS:
                    break
                amount = min(available, order.qty)
                available -= amount
                register_fill(order, amount, t, "exact")

    def hedge(t, book):
        used = {}
        for grid in list(grids.values()):
            if grid.status != "hedge_wait":
                continue
            lot = grid.lot
            if t < lot.fill_ms + hedge_latency_ms:
                continue
            x_side = "sell" if grid.side == "short" else "buy"
            px = executable(book, x_side, lot.qty, used)
            if px is None:
                stats["hedge_insufficient_xau_depth_events"] += 1
                continue
            lot.xau_px, lot.hedge_ms = px, t
            grid.status = "open"
            stats["hedge_executed"] += 1
            delay = t - lot.fill_ms
            stats["max_observed_hedge_delay_ms"] = max(stats["max_observed_hedge_delay_ms"], delay)
            log("XAU_HEDGE", t, grid, xau_side=x_side, xau_px=px,
                qty_oz=lot.qty, delay_ms=delay)

    def get_refs(t):
        x, p = open_books["XAU"], open_books["PAXG"]
        if x is None or p is None:
            return None
        if not (0 <= t - x.ts <= max_age_ms and 0 <= t - p.ts <= max_age_ms):
            return None
        return spread_refs(x, p, qty)

    def quote_evaluate(t):
        nonlocal realized_gross, peak, drawdown, last_drawdown_sample, last_valid_mark
        x, p = open_books["XAU"], open_books["PAXG"]
        refs = get_refs(t)
        if refs is None:
            stats["stale_or_missing_paired_quotes"] += 1
            return
        zones_for_observed(center, step, refs, grids, max_levels)
        used = {"XAU": {}, "PAXG": {}}
        # Close only after hedge is already active, not in the same instant.
        for grid in list(grids.values()):
            if grid.status != "open":
                continue
            lot = grid.lot
            if t <= lot.hedge_ms:
                continue
            xside = "buy" if grid.side == "short" else "sell"
            pside = "sell" if grid.side == "short" else "buy"
            # Peek, commit depth only if the TP is satisfied.
            xu, pu = dict(used["XAU"]), dict(used["PAXG"])
            xp = executable(x, xside, lot.qty, xu)
            pp = executable(p, pside, lot.qty, pu)
            if xp is None or pp is None:
                stats["exit_insufficient_depth_events"] += 1
                continue
            gross_oz = pnl_per_oz(grid.side, lot.paxg_px, lot.xau_px, pp, xp)
            if gross_oz + EPS < tp:
                continue
            used["XAU"], used["PAXG"] = xu, pu
            row = {"side": grid.side, "grid": grid.key, "grid_level_usd_oz": grid.level,
                   "maker_fill_utc": iso(lot.fill_ms), "hedge_utc": iso(lot.hedge_ms),
                   "exit_utc": iso(t), "qty_oz": lot.qty,
                   "paxg_entry_px": lot.paxg_px, "xau_entry_px": lot.xau_px,
                   "paxg_exit_px": pp, "xau_exit_px": xp,
                   "fill_reason": lot.fill_reason,
                   "gross_per_oz_usd": gross_oz, "gross_usd": lot.qty * gross_oz}
            closed.append(row)
            realized_gross += row["gross_usd"]
            grid.status, grid.lot, grid.reset_after_tp = "flat", None, True
            stats["tp_exits"] += 1
            log("TP_CLOSE", t, grid, gross_per_oz_usd=gross_oz, qty_oz=row["qty_oz"])
        # Cancellation is based on live spread, strictly outside; equality holds.
        for grid in list(grids.values()):
            value = refs[grid.side]
            if value is None:
                continue
            if grid.reset_after_tp and leave_zone(grid.side, value, grid.level):
                grid.reset_after_tp = False
            if grid.status == "pending" and leave_zone(grid.side, value, grid.level):
                remove_order(grid.key)
                grid.status = "flat"
                stats["cancel_spread"] += 1
                log("CANCEL_SPREAD", t, grid, reference=value)
        # No 'armed' requirement after cancellation: immediate next re-qualification.
        for grid in list(grids.values()):
            value = refs[grid.side]
            if (grid.status != "flat" or grid.reset_after_tp or value is None or
                    not qualifies(grid.side, value, grid.level)):
                continue
            action = "BUY" if grid.side == "short" else "SELL"
            limit, visible = (p.bid[0] if action == "BUY" else p.ask[0])
            gkey = group_key(action, limit)
            if gkey not in groups:
                groups[gkey] = Group(action, limit, visible)
            groups[gkey].orders.append(Pending(grid.key, action, limit, qty, t))
            grid.status = "pending"
            stats["maker_posts"] += 1
            log("POST", t, grid, reference=value,
                maker_action=action, maker_limit=limit,
                queue_ahead_oz=groups[gkey].ahead)
        # Sample complete-equity drawdown (fee-free) at most once per minute.
        if last_drawdown_sample is None or t - last_drawdown_sample >= snapshot_dd_every_ms:
            last_drawdown_sample = t
            mark_used = {"XAU": {}, "PAXG": {}}
            marked = 0.
            complete = True
            for grid in grids.values():
                if grid.status not in ("open", "hedge_wait"):
                    continue
                lot = grid.lot
                pside = "sell" if grid.side == "short" else "buy"
                pp = executable(p, pside, lot.qty, mark_used["PAXG"])
                if pp is None:
                    complete = False
                    break
                if grid.status == "hedge_wait":
                    marked += (pp - lot.paxg_px) * lot.qty if grid.side == "short" else (lot.paxg_px - pp) * lot.qty
                else:
                    xside = "buy" if grid.side == "short" else "sell"
                    xp = executable(x, xside, lot.qty, mark_used["XAU"])
                    if xp is None:
                        complete = False
                        break
                    marked += pnl_per_oz(grid.side, lot.paxg_px, lot.xau_px, pp, xp) * lot.qty
            if complete:
                equity = realized_gross + marked
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
                last_valid_mark = equity
                stats["complete_equity_mark_samples"] += 1
            else:
                stats["incomplete_equity_mark_samples"] += 1

    for t, kind, obj in timeline:
        if t >= test_end:
            break
        if t < test_start:
            continue
        last_ts = t
        if kind == 2:
            on_trade(t, obj)
            continue
        if kind == 0:
            open_books["XAU"] = obj
            if obj is not None:
                hedge(t, obj)
        else:
            open_books["PAXG"] = obj
        if obj is None:
            stats["invalid_book_updates"] += 1
            continue
        quote_evaluate(t)
        max_nonflat = max(max_nonflat, sum(g.status != "flat" for g in grids.values()))
        max_exposure_oz = max(max_exposure_oz, sum(g.lot.qty for g in grids.values()
                                                  if g.lot is not None))

    if last_ts is None:
        raise ValueError("No usable events in test period")
    pending_at_end = sum(g.status == "pending" for g in grids.values())
    terminal = []
    xu, pu = {}, {}
    for grid in grids.values():
        if grid.status not in ("open", "hedge_wait"):
            continue
        lot = grid.lot
        x, p = open_books["XAU"], open_books["PAXG"]
        if p is not None and last_ts - p.ts > max_age_ms:
            p = None
        if x is not None and last_ts - x.ts > max_age_ms:
            x = None
        x_side = "buy" if grid.side == "short" else "sell"
        p_side = "sell" if grid.side == "short" else "buy"
        pp = executable(p, p_side, lot.qty, pu) if p is not None else None
        xp = (executable(x, x_side, lot.qty, xu)
              if grid.status == "open" and x is not None else None)
        if pp is None or (grid.status == "open" and xp is None):
            terminal.append({"grid": grid.key, "side": grid.side,
                             "status": "unmarked", "qty_oz": lot.qty,
                             "gross_usd": None})
            continue
        gross = (pnl_per_oz(grid.side, lot.paxg_px, lot.xau_px, pp, xp) * lot.qty
                 if grid.status == "open" else
                 ((pp - lot.paxg_px) * lot.qty if grid.side == "short"
                  else (lot.paxg_px - pp) * lot.qty))
        terminal.append({"grid": grid.key, "side": grid.side, "status": grid.status,
                         "qty_oz": lot.qty, "gross_usd": gross,
                         "paxg_entry_px": lot.paxg_px, "xau_entry_px": lot.xau_px,
                         "paxg_exit_px": pp, "xau_exit_px": xp,
                         "maker_fill_utc": iso(lot.fill_ms), "hedge_utc": iso(lot.hedge_ms) if lot.hedge_ms else None})
    return {"qty_per_grid_oz": qty, "tp_gross_usd_per_oz": tp,
            "center_usd_oz": center, "grid_step_usd_oz": step,
            "hedge_latency_target_ms": hedge_latency_ms,
            "maker_posts": stats["maker_posts"], "maker_cancels_spread": stats["cancel_spread"],
            "maker_paper_fills": stats["maker_paper_fills"],
            "paper_filled_oz": stats["maker_paper_filled_milli_oz"] / 1000.,
            "paper_exact_fills": stats["exact_fills"],
            "paper_through_fills": stats["through_fills"],
            "xau_hedges": stats["hedge_executed"], "tp_closed": stats["tp_exits"],
            "terminal_pending_orders": pending_at_end,
            "terminal_open_hedged": sum(g.status == "open" for g in grids.values()),
            "terminal_unhedged_paxg": sum(g.status == "hedge_wait" for g in grids.values()),
            "max_observed_hedge_delay_ms": stats["max_observed_hedge_delay_ms"],
            "max_nonflat_grids": max_nonflat, "max_paxg_filled_exposure_oz": max_exposure_oz,
            "max_sampled_fee_free_mark_drawdown_usd": drawdown if stats["complete_equity_mark_samples"] else None,
            "complete_equity_mark_samples": stats["complete_equity_mark_samples"],
            "incomplete_equity_mark_samples": stats["incomplete_equity_mark_samples"],
            "invalid_book_updates": stats["invalid_book_updates"],
            "stale_or_missing_paired_quotes": stats["stale_or_missing_paired_quotes"],
            "eligible_public_prints": stats["eligible_public_prints"],
            "max_levels_short": sum(g.side == "short" for g in grids.values()),
            "max_levels_long": sum(g.side == "long" for g in grids.values()),
            "closed_gross_usd": sum(r["gross_usd"] for r in closed)}, closed, terminal, events, grids


def reprice_scenario(base, closed, terminal, name, fees):
    maker_bps, taker_bps = fees
    c = [{**r} for r in closed]
    term = [{**r} for r in terminal]
    for r in c:
        outcome = fee_reprice(r, maker_bps, taker_bps)
        r["net_usd"], r["fees_usd"] = outcome
    for r in term:
        outcome = fee_reprice(r, maker_bps, taker_bps) if r["gross_usd"] is not None else None
        r["marked_liquidation_net_usd"] = outcome[0] if outcome else None
        r["estimated_fees_usd"] = outcome[1] if outcome else None
    realized = sum(r["net_usd"] for r in c)
    marked = sum(r["marked_liquidation_net_usd"] for r in term if r["marked_liquidation_net_usd"] is not None)
    unmarked = sum(r["marked_liquidation_net_usd"] is None for r in term)
    result = {**base, "fee_profile": name, "maker_fee_bps": maker_bps,
              "taker_fee_bps": taker_bps,
              "closed_net_usd": round(realized, 6),
              "terminal_marked_usd": round(marked, 6) if not unmarked else None,
              "total_net_after_terminal_liquidation_usd": round(realized + marked, 6) if not unmarked else None,
              "terminal_unmarked_positions": unmarked,
              "note": "Public tape paper fills conditional; fee-derived paths unchanged"}
    return result, c, term


def write_csv(path, rows, fallback):
    cols = list(dict.fromkeys(k for row in rows for k in row)) if rows else fallback
    with Path(path).open("w", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        if rows:
            w.writerows(rows)


def run(source, old_source, output, *, bin_width=1., step=1., qtys=(.5, 1.),
        tps=(2., 3.), max_age_ms=3000, max_levels=64):
    if (not math.isfinite(bin_width) or not math.isfinite(step) or
        bin_width <= 0 or step <= 0 or max_age_ms <= 0 or max_levels < 1):
        raise ValueError("Histogram/grid/quote-age/max-level values must be positive")
    root, old, out = Path(source), Path(old_source), Path(output)
    out.mkdir(parents=True, exist_ok=True)
    print("VERIFY_MANIFEST", flush=True)
    records, manifest = get_manifest(root)
    center_report, hist = calibration_distribution(root, records, bin_width, max_age_ms)
    center = center_report["center_usd_oz"]
    print("CALIBRATION_7D", json.dumps(center_report), flush=True)
    tape, tape_quality = load_taker_tape(old)
    tape_test = [t for t in tape if TRAIN_END <= t.ts < TEST_END]
    if not tape_test:
        raise RuntimeError("Missing PUBLIC PAXG tape in TEST period; do not interpret 0 fills")
    results, all_closed, all_terminal, all_events, levels = [], [], [], [], []
    parsing_quality = None
    for qty in qtys:
        for tp in tps:
            counters = Counter()
            print("REPLAY", qty, tp, "hedge_latency_ms=2000", flush=True)
            raw, closed, terminal, events, grids = replay(
                merge_timeline(root, records, tape_test, counters), center, step, qty, tp,
                max_age_ms=max_age_ms, max_levels=max_levels,
                record_events=(qty == qtys[0] and tp == tps[0]))
            for sym in ("XAU", "PAXG"):
                if counters[sym + "_rows_seen"] != manifest["snapshots_per_symbol"][sym]:
                    raise RuntimeError(f"Missing raw 1s source rows for {sym}")
            observed = {s: {"raw": counters[s + "_rows_seen"],
                            "valid": counters[s + "_parsed"],
                            "invalid": counters[s + "_rejected"]} for s in ("XAU", "PAXG")}
            if parsing_quality is None:
                parsing_quality = observed
            elif observed != parsing_quality:
                raise RuntimeError("Inconsistent 1s quality across paths")
            print("PATH_RESULT", qty, tp, raw["maker_posts"], raw["maker_paper_fills"],
                  raw["xau_hedges"], raw["tp_closed"], flush=True)
            for name, fees in FEE_PROFILES.items():
                summary, c, pos = reprice_scenario(raw, closed, terminal, name, fees)
                results.append(summary)
                all_closed.extend([{**r, "fee_profile": name, "scenario_qty": qty, "scenario_tp": tp} for r in c])
                all_terminal.extend([{**r, "fee_profile": name, "scenario_qty": qty, "scenario_tp": tp} for r in pos])
            if events:
                all_events.extend([{**e, "scenario_qty": qty, "scenario_tp": tp} for e in events])
            levels.extend([{"qty": qty, "tp": tp, "side": g.side, "level_number": g.number,
                            "threshold_usd_oz": g.level} for g in grids.values()])
    report = {"experiment": "MODE_CENTER_7D_GRID_1S_DEVELOPMENT_REPLAY_NOT_FRESH_FORWARD",
              "test_start_utc": iso(TRAIN_END), "test_end_exclusive_utc": iso(TEST_END),
              "calibration": center_report, "source_quality": manifest,
              "test_book_integrity": parsing_quality, "tape_quality": tape_quality,
              "test_tape_count": len(tape_test),
              "fixed_hedge_latency_ms": HEDGE_LATENCY_MS,
              "config": {"hist_bin_width_usd": bin_width, "grid_step_usd": step,
                         "quote_max_age_ms": max_age_ms, "max_grid_levels_each_side": max_levels,
                         "qtys": qtys, "gross_tp_usd_per_oz": tps},
              "scenarios": results,
              "caveats": [
                  "Grid step=1 USD, histogram bin=1 USD, max quote age=3s and level cap are explicit RESEARCH DEFAULTS, not user-confirmed constants.",
                  "Calibration first 7 days of previously reviewed history; test following ~8 days, NOT untouched OOS.",
                  "Histogram uses MID/MID 1-minute snapshots; maker entry eligibility is executable XAU VWAP minus PAXG maker bid/ask. The bases differ by the side-specific spread.",
                  "Price-through paper maker fills conditional on an acknowledged standing hypothetical order; no private order IDs, matching-engine queue data, cancel latency, or proven maker fills.",
                  "Exactly 2000ms hedge latency target; actual eligible first XAU snapshot delay reported separately; XAU take uses L2 VWAP with shared depth.",
                  "Maker cancel assumed immediate; trades during in-flight cancel cannot be reconstructed.",
                  "Each Grid independent one at a time. Cancels immediately allow re-post on subsequent eligibility; TP close requires leave and re-enter to reopen.",
                  "TP gross 2/3 USD per oz before fees; fees re-priced after identical execution paths. No funding, margin/liquidation, transfer fees, impact or API outages.",
                  "Drawdown reported as sampled fee-free 60s marks, not intrasecond max and not fee-adjusted; terminal liquidation hypothetical.",
                  "No live orders, no new data download. Scenario profits are independent; never add them together."],}
    write_csv(out / "histogram_7d.csv", hist, ["lower_usd_oz", "upper_usd_oz", "minutes", "fraction", "modal_bin"])
    write_csv(out / "grid_levels.csv", levels, ["qty", "tp", "side", "level_number", "threshold_usd_oz"])
    write_csv(out / "scenarios.csv", results, ["fee_profile", "closed_net_usd"])
    write_csv(out / "closed_trades.csv", all_closed, ["fee_profile", "scenario_qty", "scenario_tp"])
    write_csv(out / "terminal_positions.csv", all_terminal, ["fee_profile", "scenario_qty", "scenario_tp"])
    write_csv(out / "maker_audit_events.csv", all_events, ["event", "time_utc", "grid"])
    (out / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=list), encoding="utf8")
    print("RESULT_SUMMARY", out / "summary.json", flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-1s", default="data/lighter_1s_20261122")
    p.add_argument("--old-source", default="data/lighter_fresh_20261122")
    p.add_argument("--output", default="data/lighter_fresh_20261122/MODE_GRID_7D_1S_HEDGE2S")
    p.add_argument("--hist-bin-usd", type=float, default=1.0)
    p.add_argument("--grid-step-usd", type=float, default=1.0)
    p.add_argument("--max-quote-age-ms", type=int, default=3000)
    p.add_argument("--max-levels", type=int, default=64)
    a = p.parse_args()
    run(a.source_1s, a.old_source, a.output, bin_width=a.hist_bin_usd,
        step=a.grid_step_usd, max_age_ms=a.max_quote_age_ms, max_levels=a.max_levels)


if __name__ == "__main__":
    main()
