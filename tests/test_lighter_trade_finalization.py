import gzip
import json
import sqlite3

from scripts import diagnose_lighter_trade_finalization as m


def test_finalization_gap_is_not_missing_trade_archive(tmp_path,monkeypatch):
    base=tmp_path/"lighter"
    base.mkdir()
    minute=(1795000000000//60000)*60000
    with sqlite3.connect(base/"lighter_marketdata.sqlite3") as db:
        db.execute("create table candles(symbol text,ts_ms integer,base_volume real)")
        db.execute("create table chunks(symbol text,kind text,begin_ms integer,end_ms integer,rows_count integer)")
        for i in range(4):
            db.execute("insert into candles values('PAXG',?,1)",(minute+i*60000,))
        db.execute("insert into chunks values('PAXG','trades',?,?,1)",(minute,minute+120000))
        db.execute("insert into chunks values('PAXG','trades',?,?,0)",(minute+120000,minute+240000))
    raw=base/"trades"/"PAXG"
    raw.mkdir(parents=True)
    with gzip.open(raw/"part.jsonl.gz","wt") as out:
        for i in (0,1):
            out.write(json.dumps({"timestamp":minute+i*60000+1000,"source":"bucket"})+"\n")
    def fake_get(route,params,key):
        assert route=="/v1/lighter/trades/PAXG"
        assert params["limit"]==50
        return {"data":[],"meta":{
            "finalized_through":m.iso(minute+120000).replace("+00:00","Z"),
            "clamped_to":m.iso(minute+120000).replace("+00:00","Z"),
            "requested_end":m.iso(minute+240000).replace("+00:00","Z")}}
    monkeypatch.setenv("OXARCHIVE_API_KEY","test-not-real")
    monkeypatch.setattr(m,"get",fake_get)
    report=m.inspect(base,"PAXG",probe=True)
    assert report["zero_row_trade_chunks"]==1
    assert report["positive_candle_minutes_without_raw_trade"]==2
    assert report["unpaired_positive_candles_before_finalized_through"]==0
    assert report["unpaired_positive_candles_at_or_after_finalized_through"]==2
    assert report["raw_trade_source_counts"]=={"bucket":2}
    assert report["api_probe"]["returned_rows"]==0
