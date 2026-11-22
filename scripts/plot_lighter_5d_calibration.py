#!/usr/bin/env python3
"""Plot calibration-only executable spread signals for BOTH long and short spread."""
import argparse
import csv
import json
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots


def create_chart(summary_path, samples_path, output_path):
    report = json.loads(Path(summary_path).read_text(encoding="utf8"))
    by_qty = report["by_order_quantity"]
    quantities = list(by_qty)
    rows = {q: [] for q in quantities}
    with Path(samples_path).open(newline="", encoding="utf8") as inp:
        for row in csv.DictReader(inp):
            key = str(float(row["qty_oz_each_leg"]))
            if key in rows:
                rows[key].append(row)
    fig = make_subplots(
        rows=2, cols=1, row_heights=[0.65,0.35], vertical_spacing=0.16,
        subplot_titles=("Entry spreads by time — SHORT upper tail / LONG lower tail",
                        "First-five-day distribution of executable spread ($/oz)"),
    )
    scenarios = [
        ("short_maker", "SHORT spread / PAXG maker BUY", "maker_entry_signal_usd_oz"),
        ("short_taker", "SHORT spread / both taker", "taker_entry_signal_usd_oz"),
        ("long_maker", "LONG spread / PAXG maker SELL", "long_maker_entry_signal_usd_oz"),
        ("long_taker", "LONG spread / both taker", "long_taker_entry_signal_usd_oz"),
    ]
    per_quantity_traces = 2 * len(scenarios)
    titles = {}
    for index, qty in enumerate(quantities):
        data = rows[qty]
        if not data:
            raise ValueError("No calibration observations for " + qty)
        x = [item["utc"] for item in data]
        values = {}
        for key,label,column in scenarios:
            vals = [float(v[column]) if v[column] not in (None,"") else None for v in data]
            values[key] = vals
            fig.add_trace(go.Scattergl(
                x=x, y=vals, mode="lines", name=label,
                visible=(index == 0), legendgroup=key), row=1, col=1)
        for key,label,_ in scenarios:
            fig.add_trace(go.Histogram(
                x=[v for v in values[key] if v is not None],
                xbins={"size": 1}, opacity=0.42,
                name=label + " histogram",
                showlegend=False, visible=(index == 0),
                legendgroup=key), row=2, col=1)
        s = by_qty[qty]
        high = s["maker_signal_xau_sell_vwap_minus_paxg_best_bid"]["percentiles"]
        low = s["long_maker_signal_xau_buy_vwap_minus_paxg_best_ask"]["percentiles"]
        titles[qty] = (
            f"Five-day XAU−PAXG zones · {qty} oz | SHORT upper P90={high.get('90')}, "
            f"P95={high.get('95')} | LONG lower P5={low.get('5')}, "
            f"P10={low.get('10')} $/oz"
        )

    fig.update_layout(
        title=titles[quantities[0]],
        barmode="overlay",
        height=850,
        margin={"t":130,"b":65},
        hovermode="x unified",
        template="plotly_white",
        updatemenus=[{
            "buttons":[{"label":qty+" oz","method":"update",
                        "args":[{"visible":[idx//per_quantity_traces==i
                                            for idx in range(len(fig.data))]},
                                {"title":titles[qty]}]}
                       for i,qty in enumerate(quantities)],
            "direction":"down","x":0.0,"y":1.12,"xanchor":"left","yanchor":"top",
            "showactive":True,
        }],
        annotations=list(fig.layout.annotations) + [{
            "text":"CALIBRATION ONLY · Maker fills hypothetical · No fees, exits or held-out data",
            "showarrow":False,"xref":"paper","yref":"paper","x":1,"y":1.15,
            "xanchor":"right","font":{"size":11},
        }],
    )
    fig.update_xaxes(title_text="Time (UTC)",row=1,col=1)
    fig.update_yaxes(title_text="Entry spread ($/oz)",row=1,col=1)
    fig.update_xaxes(title_text="Entry spread ($/oz)",row=2,col=1)
    fig.update_yaxes(title_text="Sampled minutes",row=2,col=1)
    destination=Path(output_path)
    destination.parent.mkdir(parents=True,exist_ok=True)
    fig.write_html(str(destination), include_plotlyjs=True, full_html=True, auto_open=False)
    print("TWO_SIDED_CALIBRATION_CHART",destination)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--summary",required=True)
    p.add_argument("--samples",required=True)
    p.add_argument("--output",required=True)
    a=p.parse_args()
    create_chart(a.summary,a.samples,a.output)
