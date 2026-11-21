"""Offline manual trading backtest MVP. Start: streamlit run app.py"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from workbench.data import symbols,candles,synthetic
from workbench.engine import open_position,close_position,PendingOrder,limit_touch

st.set_page_config(page_title="Trading Backtest Workstation",layout="wide")
st.title("Historical Manual Trading Workstation")
st.caption("RESEARCH ONLY | UTC | Market fill = visible candle close proxy | Limit touch = assumed fill")

defaults={"assets":{},"positions":[],"pending":[],"ledger":[],"cursor":0,
          "next_id":1,"initial":10000.0,"fee":0.0}
for k,v in defaults.items():
    if k not in st.session_state:st.session_state[k]=v
S=st.session_state
assets=S.assets

def nextid():
    i=S.next_id;S.next_id+=1;return i

def timeline():
    return sorted(set().union(*(set(df.ts_ms.tolist()) for df in assets.values())))

def mark(symbol,ts):
    df=assets[symbol]
    visible=df[df.ts_ms<=ts]
    return None if visible.empty else float(visible.iloc[-1].close)

def reset():
    S.positions=[];S.pending=[];S.ledger=[];S.cursor=0;S.next_id=1

def move(target,times):
    target=max(0,min(int(target),len(times)-1))
    if target<S.cursor and (S.positions or S.pending or S.ledger):
        st.warning("Reset before rewinding trades; scenario branching not implemented")
        return
    if target>S.cursor:
        old=times[S.cursor];new=times[target]
        for symbol,df in assets.items():
            for row in df[(df.ts_ms>old)&(df.ts_ms<=new)].to_dict("records"):
                for o in S.pending:
                    if o.symbol!=symbol or not limit_touch(o,row):continue
                    o.status="ASSUMED_FILLED"
                    p=open_position(nextid(),symbol,o.side,o.qty,o.limit_price,row["ts_ms"],o.fee_rate)
                    S.positions.append(p)
                    S.ledger.append({"ts_ms":int(row["ts_ms"]),"amount":-p.qty*p.entry_price*p.fee_rate,"kind":"entry_fee","id":p.id})
    S.cursor=target

with st.sidebar:
    st.header("Data")
    upload=st.file_uploader("CSV / ZIP / SQLite / Parquet",type=["csv","zip","sqlite3","db","parquet"])
    local=st.text_input("Or local file path (read-only)")
    filename=upload.name if upload is not None else (Path(local).name if local else None)
    blob=upload.getvalue() if upload is not None else None
    if filename:
        try:
            if blob is None:
                p=Path(local).expanduser()
                if not p.is_file() or p.stat().st_size>300_000_000:raise ValueError("File unavailable or >300 MB")
                blob=p.read_bytes()
            available=symbols(filename,blob)
            selected=st.selectbox("Source symbol",available)
            alias=st.text_input("Asset alias (for formulas)",selected.replace("/","_").replace("-","_"))
            if st.button("Load asset"):
                if not alias.isidentifier():raise ValueError("Use a valid alias such as XAU or PAXG")
                assets[alias]=candles(filename,blob,selected)
                reset()
                st.success("Loaded "+alias)
        except Exception as err:st.error(str(err))
    st.write("Assets:",", ".join(assets) or "none")
    if st.button("Clear data and scenario"):
        assets.clear();reset();st.rerun()
    if not S.positions and not S.ledger:
        S.initial=st.number_input("Starting balance (USD-like)",value=float(S.initial),min_value=1.0)
    S.fee=st.number_input("Fee % / fill (assumption)",value=float(S.fee)*100,min_value=0.,step=.01,format="%.3f")/100

if not assets:
    st.info("Load historical candles. Binance and Lighter archives from the prior research are supported.")
    st.stop()

times=timeline()
st.header("UTC historical replay")
st.caption(f"Data {datetime.fromtimestamp(times[0]/1000,timezone.utc):%Y-%m-%d %H:%M} through {datetime.fromtimestamp(times[-1]/1000,timezone.utc):%Y-%m-%d %H:%M}")
c=st.columns(5)
if c[0].button("+1"):move(S.cursor+1,times)
if c[1].button("+10"):move(S.cursor+10,times)
if c[2].button("+100"):move(S.cursor+100,times)
step=c[3].number_input("Step candles",min_value=1,value=1440)
if c[4].button("Step"):move(S.cursor+step,times)
idx=st.slider("Time cursor",0,len(times)-1,S.cursor)
if idx!=S.cursor:
    move(idx,times);st.rerun()
if st.button("Reset orders and replay"):
    reset();st.rerun()
ts=times[S.cursor]
st.markdown(f"**Clock: {datetime.fromtimestamp(ts/1000,timezone.utc):%Y-%m-%d %H:%M UTC}**")
quotes={s:mark(s,ts) for s in assets}
quotes={s:p for s,p in quotes.items() if p is not None}
if not quotes:
    st.warning("No price yet");st.stop()

st.header("Charts")
chart_type=st.radio("Type",["Candlestick","Line"],horizontal=True)
for symbol in st.multiselect("Show assets",list(assets),default=list(assets)[:2]):
    d=assets[symbol];d=d[d.ts_ms<=ts].tail(1500)
    x=pd.to_datetime(d.ts_ms,unit="ms",utc=True)
    if chart_type=="Line":f=go.Figure(go.Scatter(x=x,y=d.close,mode="lines",name=symbol))
    else:f=go.Figure(go.Candlestick(x=x,open=d.open,high=d.high,low=d.low,close=d.close,name=symbol))
    f.update_layout(title=symbol,height=310,xaxis_rangeslider_visible=False)
    st.plotly_chart(f,use_container_width=True)

formula=st.text_input("Synthetic formula (aligned closes)",value=" - ".join(list(assets)[:2]) if len(assets)>1 else next(iter(assets)))
try:
    df=synthetic(formula,{s:d[d.ts_ms<=ts] for s,d in assets.items()})
    f=go.Figure(go.Scatter(x=pd.to_datetime(df.ts_ms,unit="ms",utc=True),y=df.value,mode="lines"))
    f.update_layout(title=formula,height=260)
    st.plotly_chart(f,use_container_width=True)
    st.caption("Analytical series, not executable spread. No synthetic OHLC approximation.")
except Exception as err:st.warning("Synthetic: "+str(err))

st.header("Simulated manual trading")
left,right=st.columns(2)
with left:
    with st.form("order_form"):
        symbol=st.selectbox("Instrument",list(assets))
        side=st.radio("Side",["BUY","SELL"],horizontal=True)
        qty=st.number_input("Quantity in underlying units (NOT lots)",min_value=.000001,value=.1,format="%.6f")
        typ=st.radio("Type",["MARKET","LIMIT"],horizontal=True)
        limit=st.number_input("Limit price",min_value=.000001,value=float(quotes.get(symbol,1.0)),format="%.6f")
        submit=st.form_submit_button("Send simulated order")
    if submit:
        if symbol not in quotes:st.error("No visible price")
        elif typ=="MARKET":
            p=open_position(nextid(),symbol,side,qty,quotes[symbol],ts,S.fee)
            S.positions.append(p)
            S.ledger.append({"ts_ms":ts,"amount":-qty*quotes[symbol]*S.fee,"kind":"entry_fee","id":p.id})
            st.success("ASSUMED Market fill at last visible candle close")
        else:
            S.pending.append(PendingOrder(nextid(),symbol,side,qty,limit,ts,S.fee))
            st.info("Limit pending; next candle high/low touch assumed fill")
with right:
    active=[p for p in S.positions if p.remaining>0]
    if active:
        rows=[{"ID":p.id,"Symbol":p.symbol,"Side":p.side,"Qty":p.remaining,"Entry":p.entry_price,
               "Mark":quotes.get(p.symbol),"Floating":p.floating(quotes[p.symbol]) if p.symbol in quotes else None}
              for p in active]
        st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
        with st.form("close_form"):
            chosen=st.selectbox("Close position ID",[p.id for p in active])
            p=next(p for p in active if p.id==chosen)
            closeqty=st.number_input("Partial close qty",min_value=.000001,max_value=p.remaining,value=p.remaining,format="%.6f")
            if st.form_submit_button("Close at visible candle close"):
                pnl=close_position(p,closeqty,quotes[p.symbol])
                S.ledger.append({"ts_ms":ts,"amount":pnl,"kind":"close_net","id":p.id})
                st.success(f"Close net P/L {pnl:+.4f}")
    else:st.info("No open positions")
    waiting=[o for o in S.pending if o.status=="PENDING"]
    if waiting:
        st.dataframe(pd.DataFrame([asdict(o) for o in waiting]),hide_index=True)
        cancel=st.selectbox("Cancel pending",[o.id for o in waiting])
        if st.button("Cancel selected"):
            next(o for o in waiting if o.id==cancel).status="CANCELED";st.rerun()

realized=sum(e["amount"] for e in S.ledger)
floating=sum(p.floating(quotes[p.symbol]) for p in S.positions if p.remaining and p.symbol in quotes)
balance=S.initial+realized
m=st.columns(4)
for col,title,value in zip(m,["Balance","Realized (including fees)","Floating","Equity"],
                            [balance,realized,floating,balance+floating]):
    col.metric(title,f"{value:,.4f}")
st.caption("Important: USD-like unit assumption; no quote conversion, funding, leverage, margin, bid/ask or real execution depth.")
scenario={"positions":[asdict(p) for p in S.positions],"pending":[asdict(o) for o in S.pending],
          "ledger":S.ledger,"initial":S.initial,"replay_ts_ms":ts,
          "realized":realized,"floating":floating,"equity":balance+floating,
          "limitations":["OHLC proxy fills","No funding","No margin","No options"]}
st.download_button("Export scenario JSON",json.dumps(scenario,indent=2),file_name="scenario.json")
