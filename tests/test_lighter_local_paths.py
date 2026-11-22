import sqlite3

from workbench.data import symbols_from_path,candles_from_path
from workbench.quotes import read_quotes_from_path,quote_at


def test_local_lighter_sqlite_path_reads_selected_symbol_only(tmp_path):
    path=tmp_path/"lighter_marketdata.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("create table candles(symbol text,ts_ms integer,open real,high real,low real,close real,base_volume real)")
        db.execute("create table quotes(symbol text,ts_ms integer,bid real,ask real,bid_size real,ask_size real)")
        for sym,price in (("XAU",3560.),("PAXG",3550.)):
            db.execute("insert into candles values(?,?,?,?,?,?,?)",(sym,1795287960000,price,price+1,price-1,price,1.))
            db.execute("insert into quotes values(?,?,?,?,?,?)",(sym,1795288017000,price-0.1,price+0.2,0.5,1.1))
    assert symbols_from_path(path)==["PAXG","XAU"]
    candle=candles_from_path(path,"PAXG")
    assert len(candle)==1 and candle.close.iloc[0]==3550.
    q=read_quotes_from_path(path,"XAU")
    assert len(q)==1 and q.iloc[0].bid==3559.9
    assert q.iloc[0].ask_size==1.1
    assert quote_at(q,1795287960000)[0] is None # no look-ahead
    row,age,fresh=quote_at(q,1795288020000)
    assert fresh and age==3000 and row.bid>0
