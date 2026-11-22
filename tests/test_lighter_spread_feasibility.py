import json
import gzip

from scripts import analyze_lighter_spread_feasibility as a

def test_l2_vwap_and_no_future():
    levels=((4000.,0.25),(3998.,0.75),(3990.,1.))
    assert abs(a.vwap(levels,1.)-3998.5)<1e-10
    assert a.vwap(levels,10.) is None
    b=a.parse_book({"timestamp":"2026-10-23T01:00:30Z",
                    "bids":[{"px":"4030","sz":"1"}],
                    "asks":[{"px":"4031","sz":"1"}]})
    assert a.asof([b.ts],[b],b.ts-1,120000) is None
    assert a.asof([b.ts],[b],b.ts+120000,120000)==b
    assert a.asof([b.ts],[b],b.ts+120001,120000) is None

def test_entry_exit_spreads_and_fees():
    x=a.Book(1000000,((4030.,2.),),((4031.,2.),))
    p=a.Book(1000000,((4000.,2.),),((4002.,2.),))
    r=a.executable((1000000,x,p),1.)
    assert r["maker_signal"]==30
    assert r["taker_signal"]==28
    assert r["exit_spread"]==31
    assert a.fees(r,r,1.,0,0,"maker_assumed")==0
    assert abs(a.fees(r,r,1.,2,0,"taker")-(4030+4002+4031+4000)*2/10000)<1e-8
    assert abs(a.fees(r,r,1.,2,0,"maker_assumed")-(4030+4031+4000)*2/10000)<1e-8

def test_one_active_position_and_take_profit():
    start=1000000
    def row(t,entry,exitspread):
        return {"t":t,"maker_signal":entry,"taker_signal":entry-1,
                "exit_spread":exitspread,
                "x_sell":4030.,"x_buy":4030.,"p_sell":4030.-exitspread,
                "p_bid":4030.-entry,"p_buy":4031.-entry}
    rows=[row(start,30.,30.),row(start+60000,30.,27.),
          row(start+120000,30.,30.),row(start+180000,30.,25.)]
    r=a.simulate(rows,1.,29.,2.,60,"maker_assumed",0.,0.)
    assert r["closed_trades"]==2
    assert r["take_profit_exits"]==2
    assert r["net_usd"]==8.

def test_realistic_jsonl_fixture(tmp_path):
    begin=a.ms("2026-10-23T01:00:00Z")
    root=tmp_path/"data"
    for symbol in ("XAU","PAXG"):
        folder=root/"book"/symbol
        folder.mkdir(parents=True)
        with gzip.open(folder/"one.jsonl.gz","wt") as f:
            for n in range(6):
                t=begin+n*60000-10000
                price=(4030. if symbol=="XAU" else 4000.)
                data={"timestamp":a.iso(t),"bids":[{"px":str(price),"sz":"1"}],
                      "asks":[{"px":str(price+1),"sz":"1"}]}
                f.write(json.dumps(data)+"\n")
    out=tmp_path/"result"
    report=a.main(["--input",str(root),"--output",str(out),
                   "--start","2026-10-23T01:00:00Z",
                   "--end","2026-10-23T01:05:00Z",
                   "--quantities","0.1","--grids","25","--compressions","1",
                   "--max-holds","2","--taker-bps","0"])
    assert report["paired_minutes"]==6
    assert report["scenario_count"]==2
    assert (out/"spread_scenarios.csv").exists()
    assert (out/"summary.json").exists()
