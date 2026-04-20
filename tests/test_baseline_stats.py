"""Unit tests for baseline.stats helpers and refresh_package_hour_stats."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline_watch.baseline import stats
from pipeline_watch.baseline.stats import (
    mean,
    percentile_window,
    refresh_package_hour_stats,
    stddev,
)
from pipeline_watch.baseline.store import PackageSnapshot


def _snap(
    *,
    package: str = "requests",
    ecosystem: str = "pypi",
    release_hour: int | None = 14,
    recorded_at: str = "2026-04-20T00:00:00Z",
) -> PackageSnapshot:
    return PackageSnapshot(
        ecosystem=ecosystem,
        package=package,
        version="1.0.0",
        maintainers=[],
        release_hour=release_hour,
        recorded_at=recorded_at,
        dependencies={},
    )


def test_mean_returns_none_for_empty() -> None:
    assert mean([]) is None


def test_mean_computes_average() -> None:
    assert mean([1, 2, 3, 4]) == 2.5


def test_stddev_returns_none_for_empty() -> None:
    assert stddev([]) is None


def test_stddev_single_sample_is_zero() -> None:
    # Design choice: 1-sample returns 0 so 2-sigma checks degrade
    # gracefully rather than crashing on NaN.
    assert stddev([14.0]) == 0.0


def test_stddev_population_formula() -> None:
    # Population stddev of [1, 2, 3, 4, 5] = sqrt(2)
    result = stddev([1, 2, 3, 4, 5])
    assert result is not None
    assert abs(result - (2 ** 0.5)) < 1e-9


def test_percentile_window_empty_returns_none() -> None:
    assert percentile_window([]) is None


def test_percentile_window_single_sample_is_point() -> None:
    assert percentile_window([17.0]) == (17.0, 17.0)


def test_percentile_window_returns_quantile_bounds() -> None:
    # For [0..10], 90% window is [0.5, 9.5] under linear interpolation.
    low, high = percentile_window(list(range(11)), width=0.90)
    assert low == pytest.approx(0.5)
    assert high == pytest.approx(9.5)


def test_percentile_window_q_boundaries_return_endpoints() -> None:
    # width=1.0 collapses to (first, last) — exercises q<=0 and q>=1 paths.
    low, high = percentile_window([3.0, 9.0, 12.0], width=1.0)
    assert low == 3.0
    assert high == 12.0


def test_percentile_window_exact_integer_index() -> None:
    # len=5, width=0.5 → low_q=0.25 → pos=1.0, hi==lo branch hit.
    low, high = percentile_window([10.0, 20.0, 30.0, 40.0, 50.0], width=0.5)
    assert low == 20.0
    assert high == 40.0


def test_refresh_package_hour_stats_writes_rows(store) -> None:
    for hour in (9, 10, 11, 10):
        store.record_snapshot(_snap(release_hour=hour, recorded_at=f"2026-04-{hour:02d}T00:00:00Z"))
    written = refresh_package_hour_stats(store, now=datetime(2026, 4, 20, tzinfo=timezone.utc))
    assert written == 1
    stat = store.get_stat("package:requests", "release_hour")
    assert stat is not None
    assert stat["sample_count"] == 4
    assert stat["mean"] == pytest.approx(10.0)


def test_refresh_package_hour_stats_skips_packages_without_hour(store) -> None:
    # A package whose only snapshot has release_hour=None contributes nothing.
    store.record_snapshot(_snap(package="noisy", release_hour=None))
    written = refresh_package_hour_stats(store)
    assert written == 0
    assert store.get_stat("package:noisy", "release_hour") is None


def test_refresh_uses_default_now(store, monkeypatch) -> None:
    """Verify the default-now branch runs (coverage for the ``or`` fallback)."""
    store.record_snapshot(_snap(release_hour=14))
    # No explicit ``now`` — stats.now defaults to datetime.now(timezone.utc).
    n = refresh_package_hour_stats(store)
    assert n == 1


def test_stats_module_exposes_quantile_helper() -> None:
    # Hit the private quantile helper directly for branch coverage.
    assert stats._quantile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert stats._quantile([1.0, 2.0, 3.0], 1.0) == 3.0
    assert stats._quantile([1.0, 2.0, 3.0], 0.5) == 2.0
