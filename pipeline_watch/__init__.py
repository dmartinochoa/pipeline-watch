"""pipeline-watch — behavioural drift detection for CI/CD pipelines.

Public surface
--------------
* ``pipeline_watch.cli.main`` — the ``pipeline_watch`` console script.
* ``pipeline_watch.detectors.supply_chain`` — the 16 supply-chain
  signals (SC-001 … SC-016) and the ``scan`` orchestrator.
* ``pipeline_watch.baseline.store.Store`` — SQLite baseline persistence.
* ``pipeline_watch.output.schema`` — the ``Finding`` dataclass and the
  JSON envelope shared with pipeline-check.

Everything else is internal. Providers' ``_fetcher`` globals are
test seams, not a stable API — subject to change between releases.
"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pipeline_watch")
except PackageNotFoundError:
    __version__ = "0.1.0"
