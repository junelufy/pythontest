"""Read-only historical top-of-book snapshots. Never invent Bid/Ask from OHLC."""
import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path
import pandas as pd

def read_quotes(filename: str, blob: bytes, symbol: str | None = None) -> pd.DataFrame:
    """Read CSV, Parquet, SQLite or ZIP. Required bid and ask, optional sizes.

    Sizes are *top-of-book displayed depth*, not traded candle volume.
    """
    ext = Path(filename).suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if Path(n).suffix.lower() in (".csv", ".parquet", ".db", ".sqlite", ".sqlite3")]
            names.sort(key=lambda n: ("quote" not in n.lower() and "book" not in n.lower(), n))
            errors = []
            for n in names:
                try:
                    return read_quotes(n, z.read(n), symbol)
                except (ValueError, KeyError, sqlite3.Error) as exc:
                    errors.append(f"{n}: {exc}")
            raise ValueError("No historical bid/ask records in ZIP. " + "; ".join(errors[:2]))
    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(blob))
    elif ext == ".parquet":
        df = pd.read_parquet(io.BytesIO(blob))
    elif ext in (".sqlite3", ".sqlite", ".db"):
        with tempfile.TemporaryDirectory(prefix="quotes_ro_") as td:
            path = Path(td) / "source.db"
            path.write_bytes(blob)
            db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                available = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
                table = next((t for t in ("quotes", "book_top", "top_of_book", "orderbook_top") if t in available), None)
                if not table:
                    raise ValueError("SQLite has no quotes/book_top table")
                df = pd.read_sql_query(f"select * from {table}", db)
            finally:
                db.close()
    else:
        raise ValueError("Quote source must be CSV, Parquet, SQLite or ZIP")
    return _normalize_quotes(df,symbol)

def _normalize_quotes(df,symbol=None):
    df.columns = [str(c).strip().lower() for c in df.columns]
    rename = {
        "timestamp":"ts_ms", "time":"ts_ms", "datetime":"ts_ms",
        "bid_price":"bid", "ask_price":"ask", "best_bid":"bid", "best_ask":"ask",
        "bid_volume":"bid_size", "bid_qty":"bid_size", "bid_quantity":"bid_size",
        "ask_volume":"ask_size", "ask_qty":"ask_size", "ask_quantity":"ask_size",
    }
    df = df.rename(columns={k:v for k,v in rename.items() if k in df and v not in df})
    for required in ("ts_ms", "bid", "ask"):
        if required not in df:
            raise ValueError(f"Missing column: {required}")
    if symbol is not None and "symbol" in df:
        df = df[df.symbol.astype(str) == symbol].copy()
    if df.empty:
        raise ValueError("No quote rows for selected symbol")
    ts = pd.to_numeric(df.ts_ms, errors="coerce")
    if ts.notna().all():
        if ts.median() < 1e11:
            ts = ts * 1000
        df["ts_ms"] = ts.astype("int64")
    else:
        times = pd.to_datetime(df.ts_ms, utc=True, errors="raise")
        df["ts_ms"] = times.astype("int64") // 1_000_000
    for col in ("bid", "ask", "bid_size", "ask_size"):
        if col not in df:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if df[["bid", "ask"]].isna().any().any() or (df.bid <= 0).any() or (df.ask < df.bid).any():
        raise ValueError("Invalid historical quotes: bid/ask missing, crossed or nonpositive")
    if ((df.bid_size.dropna() < 0).any() or (df.ask_size.dropna() < 0).any()):
        raise ValueError("Negative book size")
    return df[["ts_ms", "bid", "ask", "bid_size", "ask_size"]].sort_values("ts_ms").drop_duplicates("ts_ms", keep="last").reset_index(drop=True)

def quote_at(df: pd.DataFrame | None, ts_ms: int, max_age_ms: int = 120000):
    """Last snapshot as of replay time, but never from the future.

    Returns (record|None, age_ms|None, is_fresh).
    """
    if df is None or df.empty:
        return None, None, False
    ix = int(df.ts_ms.searchsorted(ts_ms, side="right")) - 1
    if ix < 0:
        return None, None, False
    row = df.iloc[ix]
    age = int(ts_ms - row.ts_ms)
    return row, age, age <= max_age_ms

def read_quotes_from_path(path,symbol=None):
    """Read only the chosen symbol from a large completed local SQLite DB."""
    p=Path(path).expanduser().resolve()
    if not p.is_file(): raise FileNotFoundError(str(p))
    if p.suffix.lower() not in (".sqlite3",".sqlite",".db"):
        if p.stat().st_size>300_000_000: raise ValueError("Quote source >300 MB")
        return read_quotes(p.name,p.read_bytes(),symbol)
    with sqlite3.connect(p.as_uri()+"?mode=ro",uri=True) as db:
        tables={r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        table=next((t for t in ("quotes","book_top","top_of_book","orderbook_top") if t in tables),None)
        if not table: raise ValueError("SQLite has no historical quotes")
        cols={r[1] for r in db.execute(f"pragma table_info({table})")}
        if symbol is not None and "symbol" in cols:
            df=pd.read_sql_query(f"select * from {table} where symbol=? order by ts_ms",db,params=(symbol,))
        else:
            df=pd.read_sql_query(f"select * from {table} order by ts_ms",db)
    return _normalize_quotes(df,symbol)
