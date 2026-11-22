from scripts.analyze_lighter_spread_feasibility import quantiles

def test_percentile_interpolation_and_singleton():
    q=quantiles([0,10,20,30,40])
    assert q["50"]==20
    assert q["25"]==10
    assert q["75"]==30
    assert q["99"]==39.6
    assert quantiles([])=={}
    assert quantiles([17])["99.9"]==17
