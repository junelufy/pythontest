import sqlite3
import zipfile
from scripts.create_lighter_review import package

def test_review_zip(tmp_path):
    base=tmp_path/"dataset"
    base.mkdir()
    db=sqlite3.connect(base/"lighter_marketdata.sqlite3")
    db.execute("create table quotes(symbol text,ts_ms integer,bid real,ask real,bid_size real,ask_size real)")
    db.execute("create table candles(symbol text,ts_ms integer,open real,high real,low real,close real,base_volume real)")
    db.execute("create table funding(symbol text,ts_ms integer,rate text,raw_json text)")
    db.execute("create table chunks(symbol text,kind text,begin_ms integer,end_ms integer,rows_count integer)")
    db.execute("insert into quotes values('XAU',1795000000000,4000,4001,1,2)")
    db.commit()
    db.close()
    dest=tmp_path/"review.zip"
    report=package(base,dest)
    assert report["quotes"][0]["rows"]==1
    with zipfile.ZipFile(dest) as z:
        assert "sample_quotes.csv" in z.namelist()
        assert "data_coverage.json" in z.namelist()
