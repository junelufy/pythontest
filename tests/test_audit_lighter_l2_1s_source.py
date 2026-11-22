"""No-network fixtures validating 1-second raw source spot-check."""
import gzip,hashlib,json
from datetime import timedelta
from pathlib import Path
import pytest
from scripts import audit_lighter_l2_1s_source as a

def test_missing_boundary_second_and_checksum(tmp_path):
    start=a.dt("2026-11-04T01:00:00Z")
    dest=tmp_path/"l2_1s"/"XAU"/"example.jsonl.gz"
    dest.parent.mkdir(parents=True)
    with gzip.open(dest,"wt",encoding="utf8") as f:
        for sec in range(1,900):
            f.write(json.dumps({"timestamp":a.stamp(start+timedelta(seconds=sec)),
                                "bids":[["4000","1"]],"asks":[["4001","1"]]})+"\n")
    row={"symbol":"XAU","start":a.stamp(start),
         "end":a.stamp(start+timedelta(seconds=900)),
         "file":str(dest),"row_count":"899",
         "sha256":hashlib.sha256(dest.read_bytes()).hexdigest()}
    result=a.check_one(tmp_path,row)
    assert result["rows"]==899
    assert result["manifest_count_matches"]
    assert result["missing_seconds"]==["2026-11-04T01:00:00Z"]
    assert result["unique_top_of_book_states"]==1
    return None

def test_does_not_mistake_checksum_failure_for_market_gap(tmp_path):
    dest=tmp_path/"l2_1s"/"PAXG"/"x.jsonl.gz"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"bad")
    row={"symbol":"PAXG","file":"x.jsonl.gz","sha256":"wrong"}
    with pytest.raises(RuntimeError,match="checksum"):
        a.check_one(tmp_path,row)

def test_utc_parse_unix_milliseconds():
    assert a.dt("2026-11-04T01:00:00Z")==a.dt(1793754000000)
