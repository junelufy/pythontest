"""Pure accounting model. Market fills at visible candle close are only proxies."""
from dataclasses import dataclass

@dataclass
class Position:
    id: int
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_ts: int
    fee_rate: float = 0.0
    closed_qty: float = 0.0
    realized: float = 0.0
    @property
    def remaining(self):
        return max(0.0, self.qty-self.closed_qty)
    def floating(self, price):
        return (1 if self.side=="BUY" else -1)*self.remaining*(price-self.entry_price)

@dataclass
class PendingOrder:
    id: int
    symbol: str
    side: str
    qty: float
    limit_price: float
    created_ts: int
    fee_rate: float=0.0
    status: str="PENDING"

def open_position(id, symbol, side, qty, price, ts, fee_rate=0.0):
    if side not in ("BUY","SELL") or qty<=0 or price<=0 or fee_rate<0:
        raise ValueError("Invalid order")
    return Position(id,symbol,side,float(qty),float(price),int(ts),float(fee_rate))

def close_position(p, qty, price):
    if qty<=0 or qty>p.remaining+1e-9 or price<=0:
        raise ValueError("Invalid closing quantity/price")
    gross=(1 if p.side=="BUY" else -1)*qty*(price-p.entry_price)
    fee=qty*price*p.fee_rate
    p.closed_qty=min(p.qty,p.closed_qty+qty)
    p.realized+=gross-fee
    return gross-fee

def limit_touch(o, row):
    if o.status!="PENDING" or row["ts_ms"]<=o.created_ts:
        return False
    return row["low"]<=o.limit_price if o.side=="BUY" else row["high"]>=o.limit_price
