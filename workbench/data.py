"""Read-only historical OHLC import; no exchange connectivity."""
import ast
import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path
import pandas as pd

def _sqlite(blob, symbol=None):
    # Original snapshots can use WAL journal mode; deserialize() may fail.
    # Use an isolated read-only temp copy instead.
    with tempfile.TemporaryDirectory(prefix="backtest_db_") as tmp:
        path=Path(tmp)/"source.sqlite3"
        path.write_bytes(blob)
        conn=sqlite3.connect(f"file:{path}?mode=ro&immutable=1",uri=True)
        try:
            if symbol is None:
                return sorted([r[0] for r in conn.execute("select distinct symbol from candles")])
            return pd.read_sql_query(
                "select ts_ms,open,high,low,close from candles where symbol=? order by ts_ms",
                conn,params=(symbol,))
        finally:
            conn.close()

def _source(name, blob):
    ext=Path(name).suffix.lower()
    if ext==".zip":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names=[n for n in z.namelist() if n.lower().endswith((".sqlite3",".db",".csv",".parquet"))]
            if not names: raise ValueError("ZIP has no supported data file")
            names.sort(key=lambda n:(not n.lower().endswith((".db",".sqlite3")), n))
            return _source(names[0],z.read(names[0]))
    if ext in (".db",".sqlite3",".sqlite"):return ("sqlite",blob)
    if ext==".csv":return ("table",pd.read_csv(io.BytesIO(blob)))
    if ext==".parquet":return ("table",pd.read_parquet(io.BytesIO(blob)))
    raise ValueError("Expected CSV, ZIP, SQLite or Parquet")

def symbols(name,blob):
    kind,data=_source(name,blob)
    if kind=="sqlite":return _sqlite(data)
    return sorted(data.symbol.dropna().astype(str).unique().tolist()) if "symbol" in data else ["ASSET"]

def candles(name,blob,symbol):
    kind,data=_source(name,blob)
    if kind=="sqlite":df=_sqlite(data,symbol)
    else:
        df=data.copy()
        df.columns=[str(c).lower().strip() for c in df.columns]
        if "symbol" in df:df=df[df.symbol.astype(str)==symbol]
    if "timestamp" in df and "ts_ms" not in df:df=df.rename(columns={"timestamp":"ts_ms"})
    if "time" in df and "ts_ms" not in df:df=df.rename(columns={"time":"ts_ms"})
    for c in ("ts_ms","open","high","low","close"):
        if c not in df:raise ValueError("Missing "+c)
    if not pd.api.types.is_numeric_dtype(df.ts_ms):
        df["ts_ms"]=pd.to_datetime(df.ts_ms,utc=True).astype("int64")//1000000
    elif df.ts_ms.median()<1e11:df["ts_ms"]=df.ts_ms*1000
    for c in ("open","high","low","close"):df[c]=pd.to_numeric(df[c],errors="raise")
    df=df.sort_values("ts_ms").drop_duplicates("ts_ms").reset_index(drop=True)
    if df.empty:raise ValueError("No candles for selected symbol")
    return df[["ts_ms","open","high","low","close"]]

def synthetic(expression,series):
    merged=None
    for name,df in series.items():
        d=df[["ts_ms","close"]].rename(columns={"close":name})
        merged=d if merged is None else merged.merge(d,on="ts_ms",how="inner")
    if merged is None or merged.empty:raise ValueError("No aligned timestamps")
    operations={ast.Add:lambda a,b:a+b,ast.Sub:lambda a,b:a-b,
                ast.Mult:lambda a,b:a*b,ast.Div:lambda a,b:a/b}
    def ev(n):
        if isinstance(n,ast.Expression):return ev(n.body)
        if isinstance(n,ast.Name) and n.id in series:return merged[n.id]
        if isinstance(n,ast.Constant) and type(n.value) in (float,int):return float(n.value)
        if isinstance(n,ast.BinOp) and type(n.op) in operations:
            return operations[type(n.op)](ev(n.left),ev(n.right))
        if isinstance(n,ast.UnaryOp) and isinstance(n.op,ast.USub):return -ev(n.operand)
        raise ValueError("Formula supports only asset aliases and + - * /")
    result=ev(ast.parse(expression,mode="eval"))
    if not isinstance(result,pd.Series):raise ValueError("Reference an asset")
    return pd.DataFrame({"ts_ms":merged.ts_ms,"value":result}).dropna()
