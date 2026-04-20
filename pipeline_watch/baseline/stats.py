"""Baseline statistics — mean / stddev / percentile helpers.

Called by the supply-chain detector (and, later, ci-runtime) after a
scan commits new snapshots so ``baseline_stats`` always reflects the
current history. The helpers here are pure — they take sequences and
return numbers — so the same primitives power both the stats-refresh
code path and in-detector anomaly checks.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timezone

from .store import Store


def mean(values: Sequence[float]) -> float | None:
    """Arithmetic mean of *values*, or ``None`` when empty."""
    if not values:
        return None
    return sum(values) / len(values)


def stddev(values: Sequence[float]) -> float | None:
    """Population standard deviation of *values*, or ``None`` when empty.

    Uses the population form (divide by N, not N-1) because the
    baseline *is* the full observed history for its scope; there is
    no larger "true" population we're sampling from. A single-sample
    input returns 0.0 rather than NaN so downstream 2-sigma checks
    degrade to "any deviation is suspicious" instead of crashing.
    """
    if not values:
        return None
    if len(values) == 1:
        return 0.0
    m = mean(values)
    assert m is not None
    variance = sum((v - m) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def percentile_window(values: Sequence[float], *, width: float = 0.90) -> tuple[float, float] | None:
    """Return the central *width* window of *values* as ``(low, high)``.

    Used by the "release hour outside historical window" signal. A
    90% window on the sorted samples flags release hours that fall in
    the outermost 5% on either side — the maintainer's normal
    distribution rounds off at those tails.
    """
    if not values:
        return None
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        v = sorted_vals[0]
        return v, v
    low_q = (1.0 - width) / 2.0
    high_q = 1.0 - low_q
    return _quantile(sorted_vals, low_q), _quantile(sorted_vals, high_q)


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile for sorted input — the NumPy default."""
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def refresh_package_hour_stats(store: Store, *, now: datetime | None = None) -> int:
    """Recompute ``release_hour`` mean/stddev for every tracked package.

    Returns the number of stats rows written. Callers invoke this after
    ``record_snapshot`` so a subsequent detector run sees up-to-date
    baseline_stats rows without re-scanning the full history.
    """
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    written = 0
    for ecosystem, package in store.all_packages():
        hours = store.release_hours(ecosystem, package)
        if not hours:
            continue
        store.upsert_stat(
            scope=f"package:{package}",
            metric="release_hour",
            mean=mean([float(h) for h in hours]),
            stddev=stddev([float(h) for h in hours]),
            sample_count=len(hours),
            updated_at=now_iso,
        )
        written += 1
    return written
