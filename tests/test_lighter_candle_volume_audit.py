import gzip
import json
import sqlite3

from scripts.audit_lighter_trade_candle_coverage import audit


def test_audit_classifies_empty_positive_and_missing_candle_volume(tmp_path):
    base=tmp_path/"data"
    base.mkdir()
    dbpath=base/"lighter_marketdata.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.execute("create table candles(symbol text, ts_ms integer, base_volume real)")
        db.execute("create table quotes(symbol text, ts_ms integer)")
        base_ms=(1795000000000//60000)*60000
        for n,vol in enumerate((0,5,None,2)):
            db.execute("insert into candles values(?,?,?)",("PAXG",base_ms+n*60000,vol))
            db.execute("insert into quotes values(?,?)",("PAXG",base_ms+n*60000))
    folder=base/"trades"/"PAXG"
    folder.mkdir(parents=True)
    with gzip.open(folder/"a.jsonl.gz","wt",encoding="utf8") as output:
        output.write(json.dumps({"timestamp":base_ms+3*60000+1000})+"\n")
    report=audit(base,"PAXG")
    assert report["trade_minutes_missing_candle"]==0
    assert report["candles_without_matching_trade"]==3
    assert report["candle_no_trade_zero_volume"]==1
    assert report["candle_no_trade_positive_volume"]==1
    assert report["candle_no_trade_missing_volume"]==1
    assert report["trade_present_but_candle_zero_volume"]==0
    assert report["candle_no_trade_with_quote_same_minute"]==3
    assert len(report["positive_volume_no_trade_examples_utc"])==1
    assert (base/"PAXG_CANDLE_TRADE_AUDIT.json").exists()
