from datetime import date, timedelta

from scripts.measure_india_vix_mean_reversion import (
    elevated_dates,
    forward_returns,
    trailing_percentile_ranks,
)


def _dated(values: list[float]) -> list[tuple[date, float]]:
    start = date(2020, 1, 1)
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_trailing_percentile_ranks_basic():
    # window=3 (4 points incl. today): last point is the max of its own window -> rank 1.0
    series = _dated([10.0, 10.0, 10.0, 20.0])
    ranks = trailing_percentile_ranks(series, lookback_days=3)
    assert len(ranks) == 1
    assert ranks[series[3][0]] == 1.0

    # today's value (1.0) is the window minimum -> only itself is <= it -> rank 1/4
    series2 = _dated([5.0, 5.0, 5.0, 1.0])
    ranks2 = trailing_percentile_ranks(series2, lookback_days=3)
    assert round(ranks2[series2[3][0]], 4) == 0.25


def test_trailing_percentile_ranks_omits_short_history():
    series = _dated([1.0, 2.0, 3.0])
    ranks = trailing_percentile_ranks(series, lookback_days=3)
    assert ranks == {}  # only 3 points, need lookback_days+1=4


def test_elevated_dates_thresholds_correctly():
    ranks = {date(2020, 1, 1): 0.5, date(2020, 1, 2): 0.9, date(2020, 1, 3): 1.0}
    assert elevated_dates(ranks, 0.9) == [date(2020, 1, 2), date(2020, 1, 3)]


def test_forward_returns_skips_dates_without_a_forward_point():
    spot = _dated([100.0, 110.0, 121.0, 133.1])
    dates = [spot[0][0], spot[1][0], spot[3][0]]  # last has no horizon=1 forward point
    returns = forward_returns(dates, spot, horizon_days=1)
    assert len(returns) == 2
    assert round(returns[0], 4) == 0.10
    assert round(returns[1], 4) == 0.10
