"""Offline regression tests for full 15-day 1s Lighter L2 downloader.

Fake API source only. Never makes network calls or spends 0xArchive credits.
"""
import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import download_lighter_l2_1s_full as m


def fake_response(url, api_key, timeout):
    from urllib.parse import urlsplit, parse_qs
    q = parse_qs(urlsplit(url).query)
    st, en = m.parse_utc(q["start"][0]), m.parse_utc(q["end"][0])
    symbol = urlsplit(url).path.split("/")[-2]
    n = int((en-st).total_seconds())
    rows = [{"timestamp": m.iso(st + m.timedelta(seconds=i)),
             "bids": [["4000","20"]], "asks": [["4000.1","20"]],
             "symbol":symbol} for i in range(n)]
    return {"success": True, "data":rows, "meta":{"has_more":False,"request_id":"R1"}}


class Tests(unittest.TestCase):
    def test_price_provider_unix_and_iso_timestamps(self):
        self.assertEqual(m.parse_utc("2026-11-04T01:00:00Z"), m.parse_utc(1793754000000))

    def test_15d_window_calculation(self):
        s,e = m.parse_utc(m.WINDOW_START),m.parse_utc(m.WINDOW_END)
        chunks=list(m.all_windows(s,e,15))
        self.assertEqual(len(chunks),2872)
        self.assertEqual(chunks[0][0],"XAU")
        self.assertEqual(chunks[-1][0],"PAXG")
        self.assertEqual(chunks[-1][2],e)

    def test_integrity_validation_rejects_duplicate_and_wrong_schema(self):
        a,b=m.parse_utc(m.WINDOW_START),m.parse_utc(m.WINDOW_START)+m.timedelta(minutes=1)
        row={"timestamp":m.iso(a),"bids":[],"asks":[]}
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            m.validate_rows([row,row],a,b)
        with self.assertRaisesRegex(ValueError,"schema"):
            m.validate_rows([{"timestamp":m.iso(a)}],a,b)

    def test_half_open_boundary(self):
        a=m.parse_utc(m.WINDOW_START);b=a+m.timedelta(minutes=1)
        row={"timestamp":m.iso(b),"bids":[],"asks":[]}
        data,q=m.validate_rows([row],a,b)
        self.assertEqual(data,[])
        self.assertEqual(q["excluded_end_boundary"],1)

    def test_pagination_no_missing_or_cyclic_cursor(self):
        class A: max_credits=12; depth=20; max_pages=5; timeout=10
        a=m.parse_utc(m.WINDOW_START); b=a+m.timedelta(minutes=1)
        calls=[]
        def response(url,key,timeout):
            calls.append(url)
            if "cursor=NEXT" in url:
                return {"data":[{"timestamp":m.iso(a+m.timedelta(seconds=1)), "bids":[], "asks":[]}],
                        "meta":{"has_more":False}}
            return {"data":[{"timestamp":m.iso(a), "bids":[], "asks":[]}],
                    "meta":{"has_more":True, "next_cursor":"NEXT"}}
        with tempfile.TemporaryDirectory() as d, patch.object(m,"get_json",side_effect=response):
            ledger=Path(d)/"ledger.jsonl"; used=[0]
            rows,stats=m.fetch_pages(a,b,"XAU",A(),"fake",ledger,used)
            self.assertEqual((len(rows),stats["pages"],used[0]),(2,2,2))
            self.assertEqual(m.ledger_attempts(ledger),2)

    def test_credit_cap_stops_before_extra_call(self):
        class A: max_credits=1; depth=20; max_pages=4; timeout=10
        a=m.parse_utc(m.WINDOW_START);b=a+m.timedelta(minutes=1)
        def response(url,key,timeout):
            return {"data":[],"meta":{"has_more":True,"next_cursor":"MORE"}}
        with tempfile.TemporaryDirectory() as d,patch.object(m,"get_json",side_effect=response):
            with self.assertRaisesRegex(RuntimeError,"CREDIT_CAP_REACHED"):
                m.fetch_pages(a,b,"XAU",A(),"fake",Path(d)/"ledger",[0])

    def test_full_collection_and_resume_without_new_requests(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"out"; zipfile_path=Path(d)/"summary.zip"
            args=["--start","2026-11-04T01:00:00Z","--end","2026-11-04T01:30:00Z",
                  "--output-root",str(root),"--summary-zip",str(zipfile_path),
                  "--max-credits","10","--min-free-gb","0"]
            with patch.dict("os.environ",{"OXARCHIVE_API_KEY":"dummy"}),patch.object(m,"get_json",side_effect=fake_response) as api:
                self.assertEqual(m.main(args),0)
                self.assertEqual(api.call_count,4)
                self.assertEqual(m.main(args),0)
                self.assertEqual(api.call_count,4)
            summary=json.loads((root/"download_summary.json").read_text())
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["credit_budget"]["attempts_in_ledger_total"],4)
            self.assertEqual(sum(x["seconds"] for x in summary["daily"]),3600)
            with zipfile.ZipFile(zipfile_path) as z:
                self.assertIn("chunks_manifest.csv",z.namelist())
            with (root/"chunks_manifest.csv").open(newline="",encoding="utf-8") as csv_f:
                rows=list(csv.DictReader(csv_f))
            self.assertEqual(len(rows),4)
            self.assertTrue(all(float(r["coverage_pct"])==100 for r in rows))

    def test_missing_key_no_requests(self):
        with tempfile.TemporaryDirectory() as d,patch.dict("os.environ",{},clear=True):
            with self.assertRaises(SystemExit):
                m.main(["--output-root",str(Path(d)/"out")])
            self.assertFalse((Path(d)/"out"/"request_attempts.jsonl").exists())


if __name__=="__main__":
    unittest.main()
