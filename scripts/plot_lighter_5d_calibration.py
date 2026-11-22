#!/usr/bin/env python3
"""Plot first 5-day XAU/PAXG L2 spread only; never plots holdout prices."""
import argparse
import csv
import json
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def create_chart(summary_path, samples_path, output_path):
    report=json.loads(Path(summary_path).read_text(encoding="utf8"))
    by_qty=report["by_order_quantity"]
    qty_names=list(by_qty)
    rows={name:[] for name in qty_names}
    with Path(samples_path).open(newline="",encoding="utf8") as file:
        for row in csv.DictReader(file):
            q=str(float(row["qty_oz_each_leg"]))
            if q in rows:
                rows[q].append(row)
    fig=make_subplots(
        rows=2,cols=1,row_heights=[0.66,0.34],vertical_spacing=0.16,
        subplot_titles=("Spread against UTC time — first 5 days ONLY",
                        "Distribution of spreads, $/oz — 1-dollar bins"),
    )
    q_titles={}
    for i,key in enumerate(qty_names):
        data=rows[key]
        if not data:
            raise ValueError("No samples for qty "+key)
        show=(i==0)
        t=[r["utc"] for r in data]
        maker=[float(r["maker_entry_signal_usd_oz"]) if r["maker_entry_signal_usd_oz"] else None for r in data]
        taker=[float(r["taker_entry_signal_usd_oz"]) if r["taker_entry_signal_usd_oz"] else None for r in data]
        fig.add_trace(go.Scattergl(x=t,y=maker,name=f"{key} oz: PAXG Maker (assumed)",
                                   mode="lines",visible=show),row=1,col=1)
        fig.add_trace(go.Scattergl(x=t,y=taker,name=f"{key} oz: Both Taker",
                                   mode="lines",visible=show),row=1,col=1)
        fig.add_trace(go.Histogram(x=[v for v in maker if v is not None],xbins={"size":1},
                                   opacity=0.57,visible=show,name=f"{key} oz Maker distribution"),
                      row=2,col=1)
        fig.add_trace(go.Histogram(x=[v for v in taker if v is not None],xbins={"size":1},
                                   opacity=0.57,visible=show,name=f"{key} oz Taker distribution"),
                      row=2,col=1)
        s=by_qty[key]["maker_signal_xau_sell_vwap_minus_paxg_best_bid"]
        perc=s["percentiles"]
        q_titles[key]=(f"XAU−PAXG spread calibration — {key} oz | Maker "
                       f"min={s['min']}, max={s['max']}, "
                       f"P50={perc.get('50')}, P75={perc.get('75')}, "
                       f"P90={perc.get('90')}, P95={perc.get('95')} $/oz")
    fig.update_layout(
        title=q_titles[qty_names[0]],
        barmode="overlay",
        height=850,
        margin={"t":115,"b":65},
        hovermode="x unified",
        template="plotly_white",
        updatemenus=[{
            "buttons":[{"label":key+" oz",
                        "method":"update",
                        "args":[{"visible":[trace//4==i for trace in range(len(fig.data))]},
                                {"title":q_titles[key]}]}
                       for i,key in enumerate(qty_names)],
            "direction":"down","x":0.0,"y":1.09,"xanchor":"left","yanchor":"top",
            "showactive":True
        }],
        annotations=list(fig.layout.annotations)+[
            {"text":"Calibration only. Maker fill is hypothetical; fees and exits excluded.",
             "showarrow":False,"xref":"paper","yref":"paper","x":1,"y":1.12,
             "xanchor":"right","font":{"size":11}}
        ],
    )
    fig.update_xaxes(title_text="UTC",row=1,col=1)
    fig.update_yaxes(title_text="Entry spread ($/oz)",row=1,col=1)
    fig.update_xaxes(title_text="Entry spread ($/oz)",row=2,col=1)
    fig.update_yaxes(title_text="Minutes observed",row=2,col=1)
    output_path=Path(output_path)
    output_path.parent.mkdir(parents=True,exist_ok=True)
    fig.write_html(str(output_path),include_plotlyjs=True,full_html=True,auto_open=False)
    print("CALIBRATION_CHART:",output_path)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--summary",required=True)
    p.add_argument("--samples",required=True)
    p.add_argument("--output",required=True)
    a=p.parse_args()
    create_chart(a.summary,a.samples,a.output)
