import io,sqlite3,zipfile
import pandas as pd
import pytest
from workbench.data import symbols,candles,synthetic
from workbench.engine import open_position,close_position,PendingOrder,limit_touch

def test_long_short_partial():
    p=open_position(1,"XAU","BUY",2,100,0,.01)
    assert p.floating(110)==20
    assert close_position(p,1,110)==8.9
    assert p.remaining==1
    s=open_position(2,"XAU","SELL",.1,3000,0)
    assert round(s.floating(2900),8)==10

def test_limit_next_candle_only():
    order=PendingOrder(1,"XAU","BUY",1,100,60000)
    assert not limit_touch(order,{"ts_ms":60000,"low":99,"high":101})
    assert limit_touch(order,{"ts_ms":120000,"low":99,"high":101})

def test_import_and_safe_synthetic():
    df=pd.DataFrame({"symbol":["A","A"],"ts_ms":[60000,120000],
                     "open":[100,100],"high":[101,102],"low":[99,99],"close":[100,101]})
    raw=df.to_csv(index=False).encode()
    assert symbols("test.csv",raw)==["A"]
    a=candles("test.csv",raw,"A")
    b=a.copy();b["close"]-=5
    assert list(synthetic("A-B",{"A":a,"B":b}).value)==[5,5]
    with pytest.raises(ValueError):synthetic("__import__('os')",{"A":a})
    db=sqlite3.connect(":memory:");df.to_sql("candles",db,index=False)
    assert len(candles("test.sqlite3",db.serialize(),"A"))==2
