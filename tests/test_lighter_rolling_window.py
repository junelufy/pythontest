"""No network/credentials required. Test rolling Free retention cutoff."""
import io
import json
import sqlite3
from urllib.error import HTTPError

from scripts import download_lighter_oxarchive as m


def test_history_window_error_cutoff_has_trailing_period(monkeypatch):
    msg = {
        "code": 403,
        "error_code": "history_window_exceeded",
        "error": (
            "Your plan includes the most recent 30 days of history: "
            "this request starts at 2026-10-22T21:36:00Z, "
            "before the earliest allowed timestamp 2026-10-22T21:57:15Z. "
            "Requests cannot be chunked across the age boundary."
        ),
        "success": False,
    }
    def failing_request(*args, **kwargs):
        raise HTTPError("test URL",403,"Forbidden",{},io.BytesIO(json.dumps(msg).encode()))
    monkeypatch.setattr(m,"urlopen",failing_request)
    try:
        m.get("/v1/lighter/orderbook/PAXG/history",{"start":1,"end":2},"placeholder-key")
        assert False, "Expected a cutoff error"
    except m.HistoryWindowExceeded as error:
        assert error.earliest_ms==m.timestamp("2026-10-22T21:57:15Z")


def test_clip_and_resume_chunk_without_losing_prior_data(tmp_path,monkeypatch):
    root=tmp_path/"new"
    root.mkdir()
    db=sqlite3.connect(root/"db.sqlite3")
    m.init(db)
    begin=m.timestamp("2026-10-22T21:36:00Z")
    finish=m.timestamp("2026-10-23T00:36:00Z")
    cutoff=m.timestamp("2026-10-22T21:57:15Z")
    calls=[]
    def provider(*args):
        calls.append(args[2])
        if len(calls)==1:
            raise m.HistoryWindowExceeded(cutoff,"history_window_exceeded")
        assert args[2]==cutoff+90000
        yield [{"timestamp":"2026-10-22T22:00:00Z",
                "bids":[["4000","1.5"]],"asks":[["4001","3.5"]]}]
    monkeypatch.setattr(m,"fetch_pages",provider)
    m.collect(db,root,"book","PAXG",begin,finish,"key","checkpoint",20)
    assert db.execute("select count(*) from quotes").fetchone()[0]==1
    assert db.execute("select actual_start_ms from history_gaps").fetchone()[0]==cutoff+90000
    m.collect(db,root,"book","PAXG",begin,finish,"key","checkpoint",20)
    assert len(calls)==2
    db.close()


def test_fully_expired_chunk_logged_as_gap(tmp_path,monkeypatch):
    root=tmp_path/"new"
    root.mkdir()
    db=sqlite3.connect(root/"db.sqlite3")
    m.init(db)
    begin=m.timestamp("2026-10-22T21:36:00Z")
    finish=begin+3600000
    def provider(*args):
        raise m.HistoryWindowExceeded(finish+1000,"history_window_exceeded")
        yield None
    monkeypatch.setattr(m,"fetch_pages",provider)
    m.collect(db,root,"book","PAXG",begin,finish,"key","checkpoint",20)
    assert db.execute("select rows_count from chunks").fetchone()[0]==0
    assert db.execute("select count(*) from history_gaps").fetchone()[0]==1
    db.close()
