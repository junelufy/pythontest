import json
import zipfile
from pathlib import Path

import pytest

from scripts import collect_lighter_targeted_1s_l3 as collector


def test_predefined_incident_windows_and_utc():
    assert len(collector.CENTERS)==4
    assert collector.fmt(collector.parse("2026-11-12T08:20:00Z"))=="2026-11-12T08:20:00Z"
    assert collector.parse("2026-11-04T05:04:00Z")<collector.parse("2026-11-12T08:20:00Z")


def test_trusted_host_and_api_key_is_never_in_url_or_output(monkeypatch):
    import urllib.parse
    url=collector.url_for("/orderbook/PAXG/history",{
        "start":"2026-11-12T08:19:00Z","end":"2026-11-12T08:20:00Z",
        "granularity":"1s"
    })
    assert urllib.parse.urlsplit(url).netloc=="api.0xarchive.io"
    assert "secret" not in url


def test_one_page_is_preserved_verbatim_and_counted(monkeypatch):
    captured=[]
    def fake_get(url,api_key):
        captured.append((url,api_key))
        return {"data":[{"timestamp":"2026-11-12T08:19:00Z","bids":[],"asks":[]}],
                "meta":{"has_more":False}}
    monkeypatch.setattr(collector,"get_json",fake_get)
    files={}
    audit=[]
    n=collector.collect_pages("/orderbook/PAXG/history",{
        "start":"2026-11-12T08:19:00Z","end":"2026-11-12T08:20:00Z",
        "granularity":"1s","limit":1000
    },"a-secret-not-for-output","case/PAXG",files,audit)
    assert n==1
    assert len(captured)==1
    assert "a-secret-not-for-output" not in json.dumps(files)
    raw=json.loads(files["case/PAXG/page_001.json"])
    assert raw["response"]["data"][0]["timestamp"]=="2026-11-12T08:19:00Z"
    assert audit[0]["records"]==1


def test_paginated_tape_never_drops_second_page(monkeypatch):
    def fake_get(url,api_key):
        if "cursor=next1" in url:
            return {"data":[{"trade_id":"t2"}],"meta":{"has_more":False}}
        return {"data":[{"trade_id":"t1"}],"meta":{"has_more":True,"next_cursor":"next1"}}
    monkeypatch.setattr(collector,"get_json",fake_get)
    files={}
    audit=[]
    count=collector.collect_pages("/trades/PAXG",
       {"start":"2026-11-12T08:19:00Z","end":"2026-11-12T08:20:00Z"},
       "test-key","test/trades",files,audit)
    assert count==2
    assert len(files)==2


def test_has_more_without_cursor_fails_closed(monkeypatch):
    monkeypatch.setattr(collector,"get_json",
                        lambda url,key: {"data":[],"meta":{"has_more":True}})
    with pytest.raises(RuntimeError,match="Pagination"):
        collector.collect_pages("/trades/PAXG",{},"x","trades",{},[])


def test_does_not_call_api_without_key(monkeypatch,tmp_path):
    monkeypatch.delenv("OXARCHIVE_API_KEY",raising=False)
    with pytest.raises(SystemExit) as ex:
        collector.main(["--output",str(tmp_path/"evidence.zip")])
    assert ex.value.code!=0
    assert not (tmp_path/"evidence.zip").exists()


def test_bad_lookaround_is_rejected(monkeypatch,tmp_path):
    monkeypatch.setenv("OXARCHIVE_API_KEY","fake")
    with pytest.raises(SystemExit):
        collector.main(["--minutes-each-side","99",
                        "--output",str(tmp_path/"bad.zip")])


def test_partial_source_outage_always_writes_zip_and_nonzero(monkeypatch,tmp_path):
    monkeypatch.setenv("OXARCHIVE_API_KEY","fake")
    monkeypatch.setattr(collector,"CENTERS",{"test":"2026-11-12T08:20:00Z"})
    def fake_collect(path,params,key,name,files,audit):
        if "orderbook" in path:raise RuntimeError("fake entitlement failure")
        files[name+"/page_001.json"]=json.dumps({"response":{"data":[]}})
        audit.append({"name":name,"records":0,"pages":1})
        return 0
    monkeypatch.setattr(collector,"collect_pages",fake_collect)
    path=tmp_path/"source.zip"
    assert collector.main(["--minutes-each-side","1","--output",str(path)])==2
    with zipfile.ZipFile(path) as z:
        r=json.loads(z.read("SUMMARY.json"))
    assert r["errors"]
    assert "8-day" in r["crucial_limit"]
