import csv
import json
from pathlib import Path

import pytest

from scripts.summarize_lighter_12d_zones import compute, FROZEN_START, DAY_MS
from scripts.analyze_lighter_spread_feasibility import iso


def fixture(tmp_path):
    summary={
        "calibration_days":12,
        "calibration_start_inclusive_utc":iso(FROZEN_START),
        "calibration_end_exclusive_utc":iso(FROZEN_START+12*DAY_MS),
        "reserved_out_of_sample_start_inclusive_utc":iso(FROZEN_START+12*DAY_MS),
        "reserved_out_of_sample_end_exclusive_utc":iso(FROZEN_START+12*DAY_MS+14*DAY_MS+23*3600000),
        "by_order_quantity":{"0.5":{"maker_signal_ticks_with_enough_l2":24}},
    }
    summary_path=tmp_path/"calibration_12d_summary.json"
    summary_path.write_text(json.dumps(summary))
    sample_path=tmp_path/"calibration_12d_spread_samples.csv"
    data=[]
    for window in range(12):
        for minute, delta in enumerate((0.,10.)):
            data.append({
                "ts_ms":FROZEN_START+window*DAY_MS+minute*60000,
                "qty_oz_each_leg":"0.5",
                "maker_entry_signal_usd_oz":str(window+delta),
                "taker_entry_signal_usd_oz":str(window+delta-1.),
                "long_maker_entry_signal_usd_oz":str(window+delta-20.),
                "long_taker_entry_signal_usd_oz":str(window+delta-19.),
            })
    with sample_path.open("w",newline="") as out:
        writer=csv.DictWriter(out,fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)
    return summary_path,sample_path


def test_twelve_exact_24h_window_zones(tmp_path):
    summary,samples=fixture(tmp_path)
    result=compute(samples,summary,tmp_path/"out")
    z=result["equal_day_weight_zones_by_qty"]["0.5"]["zones_usd_per_oz"]
    assert z["short_maker"]["mean_of_12_daily_P90"]==14.5
    assert z["long_maker"]["mean_of_12_daily_P10"]==-13.5
    assert result["days"]==12
    assert result["observations_in_csv"]==24
    assert result["calibration_end_exclusive_utc"]=="2026-11-04T01:00:00+00:00"
    assert result["reserved_test_start_inclusive_utc"]=="2026-11-04T01:00:00+00:00"
    with (tmp_path/"out"/"zone_12d_daily.csv").open(newline="") as inp:
        rows=list(csv.DictReader(inp))
    assert len(rows)==12
    assert rows[-1]["start_utc"]=="2026-11-03T01:00:00+00:00"


def test_reject_any_future_holdout_observation(tmp_path):
    summary,samples=fixture(tmp_path)
    with samples.open("a") as inp:
        inp.write(f"{FROZEN_START+12*DAY_MS},0.5,0,0,0,0\n")
    with pytest.raises(ValueError,match="outside frozen"):
        compute(samples,summary,tmp_path/"out")
