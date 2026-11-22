#!/usr/bin/env python3
"""27-day Lighter XAU/PAXG *feasibility* scan from real L2, not verified maker fills."""
import argparse
import bisect
import csv
import gzip
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

UTC=timezone.utc

def ms(value):
    if isinstance(value,(int,float)) or str(value).isdigit():
        x=float(value)
        return int(x if x>1e11 else x*1000)
    return int(datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()*1000)

def iso(value):
    return datetime.fromtimestamp(value/1000,UTC).isoformat() if value is not None else None

@dataclass(frozen=True)
class Book:
    ts:int
    bid:tuple
    ask:tuple

def parse_book(row):
    def side(name,reverse):
        levels=[]
        for level in row.get(name,[]):
            price=float(level.get("px",level.get("price"))) if isinstance(level,dict) else float(level[0])
            qty=float(level.get("sz",level.get("size",level.get("qty")))) if isinstance(level,dict) else float(level[1])
            if not (math.isfinite(price) and math.isfinite(qty) and price>0 and qty>=0):
                raise ValueError("Invalid price/quantity in "+name)
            if qty:levels.append((price,qty))
        levels.sort(reverse=reverse)
        if not levels:raise ValueError("Empty "+name)
        return tuple(levels)
    b=side("bids",True)
    a=side("asks",False)
    if b[0][0]>=a[0][0]:
        raise ValueError("Crossed order book")
    return Book(ms(row["timestamp"]),b,a)

def read_books(base,symbol,start,end):
    files=sorted((base/"book"/symbol).glob("*.jsonl.gz"))
    if not files:raise FileNotFoundError("No Lighter L2 files for "+symbol)
    data={}
    bad=0
    for file in files:
        with gzip.open(file,"rt",encoding="utf8") as f:
            for line in f:
                if not line.strip():continue
                row=json.loads(line)
                ts=ms(row["timestamp"])
                if start-180_000<=ts<=end:
                    try:data[ts]=parse_book(row)
                    except (ValueError,TypeError,KeyError) as e:
                        bad+=1
                        if bad<=3:print("WARNING",symbol,iso(ts),str(e),flush=True)
    print(symbol,"L2 snapshots:",len(data),"rejected:",bad,flush=True)
    if not data:raise ValueError("No L2 history for "+symbol)
    books=[data[t] for t in sorted(data)]
    return [b.ts for b in books],books

def vwap(levels,qty):
    remaining=qty
    notional=0.
    for price,size in levels:
        take=min(remaining,size)
        notional+=price*take
        remaining-=take
        if remaining<=1e-9:return notional/qty
    return None

def asof(times,books,t,max_age):
    index=bisect.bisect_right(times,t)-1
    if index<0:return None
    book=books[index]
    return book if 0<=t-book.ts<=max_age else None

def paired(x,p,begin,end,step_minutes,max_age):
    first=(begin+59999)//60000*60000
    result=[]
    for tick in range(first,end+1,step_minutes*60000):
        xb=asof(*x,tick,max_age)
        pb=asof(*p,tick,max_age)
        if xb is not None and pb is not None:
            result.append((tick,xb,pb))
    return result

def quantiles(values):
    """Linear-interpolation percentiles of observed per-minute executable spread."""
    if not values:
        return {}
    ordered=sorted(values)
    n=len(ordered)
    result={}
    for pct in (1,5,10,25,50,75,90,95,99,99.5,99.9):
        point=(n-1)*pct/100
        low=int(point)
        high=min(low+1,n-1)
        value=ordered[low]+(ordered[high]-ordered[low])*(point-low)
        result[str(pct)]=round(value,5)
    return result

def executable(event,qty):
    t,x,p=event
    xs=vwap(x.bid,qty)
    xb=vwap(x.ask,qty)
    ps=vwap(p.bid,qty)
    pb=vwap(p.ask,qty)
    passive=p.bid[0][0]
    return {"t":t,"x_sell":xs,"x_buy":xb,"p_sell":ps,"p_buy":pb,"p_bid":passive,
            "maker_signal":xs-passive if xs is not None else None,
            "taker_signal":xs-pb if xs is not None and pb is not None else None,
            "exit_spread":xb-ps if xb is not None and ps is not None else None}

def fees(entry,exit,qty,taker_bps,maker_bps,mode):
    buy_p=entry["p_bid"] if mode=="maker_assumed" else entry["p_buy"]
    bps=maker_bps if mode=="maker_assumed" else taker_bps
    return qty/10000*(taker_bps*entry["x_sell"]+bps*buy_p+
                       taker_bps*exit["x_buy"]+taker_bps*exit["p_sell"])

def simulate(rows,qty,grid,compression,timeout,mode,taker_bps,maker_bps):
    signal="maker_signal" if mode=="maker_assumed" else "taker_signal"
    opened=None
    profits=[]
    peak=0.
    equity=0.
    drawdown=0.
    forced=0
    takeprofit=0
    max_late=0.
    total_gross=0.
    total_fees=0.
    for row in rows:
        if opened is None:
            if row[signal] is not None and row[signal]>=grid and row["exit_spread"] is not None:
                opened=row
            continue
        held=(row["t"]-opened["t"])/60000
        if held<1 or row["exit_spread"] is None:continue
        gross_per_oz=opened[signal]-row["exit_spread"]
        hit=gross_per_oz>=compression
        expiry=held>=timeout
        if not hit and not expiry:continue
        cost=fees(opened,row,qty,taker_bps,maker_bps,mode)
        net=qty*gross_per_oz-cost
        profits.append(net)
        total_gross+=qty*gross_per_oz
        total_fees+=cost
        equity+=net
        peak=max(peak,equity)
        drawdown=max(drawdown,peak-equity)
        if hit:takeprofit+=1
        else:forced+=1
        if expiry:max_late=max(max_late,max(0,held-timeout))
        opened=None
    n=len(profits)
    return {"mode":mode,"qty_oz":qty,"grid_usd":grid,"compression_usd":compression,
            "max_hold_minutes":timeout,"taker_fee_bps":taker_bps,"maker_fee_bps":maker_bps,
            "closed_trades":n,
            "wins_percent":round(100*sum(x>0 for x in profits)/n,2) if n else None,
            "gross_usd":round(total_gross,4),
            "fees_usd":round(total_fees,4),
            "net_usd":round(equity,4),
            "average_net_usd":round(equity/n,4) if n else None,
            "worst_closed_trade_usd":round(min(profits),4) if n else None,
            "max_realized_drawdown_usd":round(drawdown,4),
            "take_profit_exits":takeprofit,"timeout_exits":forced,
            "max_exit_delay_minutes":round(max_late,1),
            "unclosed_positions":int(opened is not None),
            "warning":"PAXG maker fill assumed without queue/trade verification" if mode=="maker_assumed"
                      else "Taker L2 VWAP fill assumed at snapshot time"}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input",required=True)
    p.add_argument("--output",default="")
    p.add_argument("--start",default="2026-10-23T01:00:00Z")
    p.add_argument("--end",default="2026-11-18T23:59:00Z")
    p.add_argument("--max-quote-age",type=int,default=120)
    p.add_argument("--step-minutes",type=int,default=1)
    p.add_argument("--quantities",type=float,nargs="+",default=[0.1,0.5,1.0])
    p.add_argument("--grids",type=float,nargs="+",default=[25,27,29,31])
    p.add_argument("--compressions",type=float,nargs="+",default=[2,5])
    p.add_argument("--max-holds",type=int,nargs="+",default=[60,240])
    p.add_argument("--taker-bps",type=float,nargs="+",default=[0,2])
    p.add_argument("--maker-bps",type=float,default=0)
    a=p.parse_args(argv)
    if (a.step_minutes<=0 or a.max_quote_age<=0 or
        not a.quantities or min(a.quantities)<=0 or
        not a.max_holds or min(a.max_holds)<=0):
        p.error("Invalid step/quote age/qty/hold")
    begin,end=ms(a.start),ms(a.end)
    if end<=begin:p.error("End must be after start")
    root=Path(a.input).expanduser().resolve()
    dest=Path(a.output).expanduser() if a.output else root/"SPREAD_FEASIBILITY"
    dest.mkdir(parents=True,exist_ok=True)
    x=read_books(root,"XAU",begin,end)
    paxg=read_books(root,"PAXG",begin,end)
    events=paired(x,paxg,begin,end,a.step_minutes,a.max_quote_age*1000)
    if not events:raise ValueError("No paired snapshots with sufficiently recent quotes")
    print("Paired snapshot minutes:",len(events),flush=True)
    rows=[]
    diagnostics=[]
    for qty in a.quantities:
        prices=[executable(event,qty) for event in events]
        maker=[r["maker_signal"] for r in prices if r["maker_signal"] is not None]
        taker=[r["taker_signal"] for r in prices if r["taker_signal"] is not None]
        exit_depth=sum(r["exit_spread"] is not None for r in prices)
        diagnostic={"qty_oz":qty,"paired_minutes":len(prices),
                    "maker_signal_depth_available":len(maker),
                    "taker_signal_depth_available":len(taker),
                    "exit_depth_available":exit_depth,
                    "maker_signal_gte_25":sum(v>=25 for v in maker),
                    "maker_signal_gte_27":sum(v>=27 for v in maker),
                    "maker_signal_gte_29":sum(v>=29 for v in maker),
                    "maker_signal_gte_31":sum(v>=31 for v in maker),
                    "taker_signal_gte_25":sum(v>=25 for v in taker),
                    "max_maker_signal":max(maker) if maker else None,
                    "max_taker_signal":max(taker) if taker else None,
                    "maker_signal_quantiles_usd_per_oz":quantiles(maker),
                    "taker_signal_quantiles_usd_per_oz":quantiles(taker),
                    "maker_signal_minutes_gte_grid":{str(v):sum(x>=v for x in maker) for v in a.grids},
                    "taker_signal_minutes_gte_grid":{str(v):sum(x>=v for x in taker) for v in a.grids}}
        diagnostics.append(diagnostic)
        print("SIGNALS:",diagnostic,flush=True)
        for mode in ("taker","maker_assumed"):
            for grid in a.grids:
                for compression in a.compressions:
                    for timeout in a.max_holds:
                        for bps in a.taker_bps:
                            rows.append(simulate(prices,qty,grid,compression,timeout,
                                                 mode,bps,a.maker_bps))
    csvfile=dest/"spread_scenarios.csv"
    with csvfile.open("w",newline="",encoding="utf8") as out:
        writer=csv.DictWriter(out,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    valid=[r for r in rows if r["closed_trades"]>=10 and not r["unclosed_positions"]]
    valid.sort(key=lambda x:x["net_usd"],reverse=True)
    traded=[r for r in rows if r["closed_trades"]>0]
    traded.sort(key=lambda x:x["net_usd"],reverse=True)
    summary={"study":"27-day XAU/PAXG L2 conditional feasibility, NOT verified fill backtest",
             "start_utc":iso(begin),"end_utc":iso(end),
             "paired_minutes":len(events),"first_pair_utc":iso(events[0][0]),
             "last_pair_utc":iso(events[-1][0]),
             "depth_availability":diagnostics,
             "scenario_count":len(rows),
             "best_independent_scenarios_illustrative_only":valid[:12],
             "scenarios_with_at_least_one_closed_trade":len(traded),
             "best_independent_traded_scenarios_illustrative_only":traded[:12],
             "worst_independent_traded_scenarios_illustrative_only":traded[-6:],
             "caveats":[
                "Maker PAXG best-bid entries are assumed immediately filled; this is an optimistic upper-bound, NOT actual fills.",
                "Taker fills assume all 20 levels remain executable at recorded snapshot prices.",
                "No queue priority, adverse selection, order latency, margin, liquidation, or cash funding settlements.",
                "Grid/qty/exit/fee configurations are independent: their profits cannot be added together.",
                "Exits at hypothetical spread compression target or timeout; no agreed final exit policy.",
                "Remaining open trades are NOT marked to market; inspect unclosed_positions.",
                "Requires both instruments priced per ounce; confirm contract specs and fees.",
                "Only an in-sample 27-day feasibility screen; do not annualize or call returns reliable."
              ],
              "parameters":vars(a)}
    (dest/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print("RESULT_CSV:",csvfile,flush=True)
    print("RESULT_SUMMARY:",dest/"summary.json",flush=True)
    print("WARNING: conditional feasibility only; no verified maker fills.",flush=True)
    return summary

if __name__=="__main__":main()
