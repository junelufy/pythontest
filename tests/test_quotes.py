import io
import zipfile
import pandas as pd
import pytest
from workbench.quotes import read_quotes, quote_at

RAW=b'ts_ms,symbol,bid,ask,bid_volume,ask_volume\n1793836800000,XAU,3000,3000.5,0.2,1.0\n1793836860000,XAU,3001,3001.5,0.3,0.8\n'

def test_quotes_and_freshness():
    df=read_quotes('q.csv',RAW,symbol='XAU')
    assert list(df.bid_size)==[.2,.3]
    assert quote_at(df,1793836800000)[0].bid==3000
    assert quote_at(df,1793836799000)[0] is None
    row,age,fresh=quote_at(df,1793836900000,max_age_ms=30000)
    assert row.bid==3001 and age==40000 and not fresh

def test_zip_csv():
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,'w') as z:z.writestr('quotes.csv',RAW)
    assert len(read_quotes('data.zip',mem.getvalue()))==2

def test_bad_bid_ask():
    with pytest.raises(ValueError):read_quotes('bad.csv',b'time,bid,ask\n2026-11-05T00:00:00Z,5,4\n')

def test_missing_sides_not_zero():
    df=read_quotes('q.csv',b'ts_ms,bid,ask\n1793836800000,100,101\n')
    assert pd.isna(df.iloc[0].bid_size)
    assert pd.isna(df.iloc[0].ask_size)
