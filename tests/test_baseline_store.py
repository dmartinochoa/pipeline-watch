"""Store tests — run against ``:memory:`` so the real filesystem is never touched."""
from __future__ import annotations

import sqlite3

import pytest

from pipeline_watch.baseline.store import (
    PackageSnapshot,
    Store,
    default_baseline_path,
)


@pytest.fixture
def store() -> Store:
    conn = sqlite3.connect(":memory:")
    return Store(conn)


def _snap(**overrides) -> PackageSnapshot:
    base = PackageSnapshot(
        ecosystem="pypi",
        package="requests",
        version="2.31.0",
        maintainers=[{"name": "alice", "email": "a@example.com", "first_seen": "2015-01-01T00:00:00Z"}],
        release_hour=14,
        release_weekday=2,
        has_install_script=False,
        install_script_hash=None,
        dependencies={"urllib3": ">=1.21.1", "certifi": ">=2017.4.17"},
        recorded_at="2026-04-20T10:00:00Z",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_schema_is_current_on_open(store: Store) -> None:
    assert store.schema_version() == 1


def test_migration_is_idempotent(store: Store) -> None:
    # Running migrate again (via the constructor's side effect) must
    # not duplicate tables or change the version.
    Store(store.conn)  # re-entry
    assert store.schema_version() == 1
    tables = {
        row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table';"
        ).fetchall()
    }
    assert {
        "package_snapshots", "pipeline_runs", "audit_events", "baseline_stats",
    }.issubset(tables)


def test_record_and_fetch_latest(store: Store) -> None:
    store.record_snapshot(_snap())
    store.record_snapshot(_snap(version="2.32.0", recorded_at="2026-04-21T10:00:00Z"))
    latest = store.latest_snapshot("pypi", "requests")
    assert latest is not None
    assert latest.version == "2.32.0"
    assert latest.dependencies == {"urllib3": ">=1.21.1", "certifi": ">=2017.4.17"}


def test_snapshots_are_append_only(store: Store) -> None:
    # Recording the same version twice preserves history; the hour
    # distribution depends on it.
    store.record_snapshot(_snap(release_hour=9))
    store.record_snapshot(_snap(release_hour=14, recorded_at="2026-04-21T10:00:00Z"))
    hours = store.release_hours("pypi", "requests")
    assert sorted(hours) == [9, 14]


def test_snapshots_for_orders_newest_first(store: Store) -> None:
    store.record_snapshot(_snap(version="2.30.0", recorded_at="2026-04-01T00:00:00Z"))
    store.record_snapshot(_snap(version="2.31.0", recorded_at="2026-04-10T00:00:00Z"))
    store.record_snapshot(_snap(version="2.32.0", recorded_at="2026-04-20T00:00:00Z"))
    rows = store.snapshots_for("pypi", "requests")
    assert [s.version for s in rows] == ["2.32.0", "2.31.0", "2.30.0"]


def test_all_packages_returns_distinct_pairs(store: Store) -> None:
    store.record_snapshot(_snap())
    store.record_snapshot(_snap(version="2.32.0", recorded_at="2026-04-21T10:00:00Z"))
    store.record_snapshot(_snap(package="flask", version="3.0.0"))
    store.record_snapshot(_snap(ecosystem="npm", package="left-pad", version="1.3.0"))
    pairs = store.all_packages()
    assert ("pypi", "requests") in pairs
    assert ("pypi", "flask") in pairs
    assert ("npm", "left-pad") in pairs
    assert len(pairs) == 3  # distinct — no dup from the two requests rows


def test_json_columns_roundtrip(store: Store) -> None:
    store.record_snapshot(_snap(
        maintainers=[{"name": "bob", "email": "b@x.com", "first_seen": "2020-01-01"}],
        dependencies={"click": ">=8,<9"},
    ))
    latest = store.latest_snapshot("pypi", "requests")
    assert latest is not None
    assert latest.maintainers == [
        {"name": "bob", "email": "b@x.com", "first_seen": "2020-01-01"}
    ]
    assert latest.dependencies == {"click": ">=8,<9"}


def test_upsert_stat_replaces_on_conflict(store: Store) -> None:
    store.upsert_stat(
        "package:requests", "release_hour",
        mean=14.0, stddev=1.5, sample_count=10,
        updated_at="2026-04-20T00:00:00Z",
    )
    store.upsert_stat(
        "package:requests", "release_hour",
        mean=13.0, stddev=2.0, sample_count=11,
        updated_at="2026-04-21T00:00:00Z",
    )
    stat = store.get_stat("package:requests", "release_hour")
    assert stat == {
        "mean": 13.0, "stddev": 2.0, "sample_count": 11,
        "last_updated": "2026-04-21T00:00:00Z",
    }
    # Only one row — conflict clause worked.
    assert len(store.all_stats()) == 1


def test_reset_scope_package(store: Store) -> None:
    store.record_snapshot(_snap())
    store.record_snapshot(_snap(package="flask", version="3.0.0"))
    store.upsert_stat(
        "package:requests", "release_hour",
        mean=14.0, stddev=0.0, sample_count=1,
        updated_at="2026-04-20T00:00:00Z",
    )
    deleted = store.reset_scope("package:requests")
    assert deleted == 1
    assert store.latest_snapshot("pypi", "requests") is None
    assert store.latest_snapshot("pypi", "flask") is not None
    assert store.get_stat("package:requests", "release_hour") is None


def test_reset_scope_rejects_unknown_prefix(store: Store) -> None:
    with pytest.raises(ValueError):
        store.reset_scope("unknown:whatever")


def test_reset_scope_job_requires_repo_and_job(store: Store) -> None:
    with pytest.raises(ValueError):
        store.reset_scope("job:missing-jobname")


def test_record_run_roundtrip(store: Store) -> None:
    rid = store.record_run(
        provider="github-actions",
        repo="myorg/myrepo",
        job_name="build",
        network_destinations=["pypi.org", "github.com"],
        secrets_accessed=["GH_TOKEN"],
        artifact_checksums={"dist.tar.gz": "abc"},
        duration_seconds=42.5,
        config_hash="cafebabe",
        triggered_at="2026-04-20T09:00:00Z",
        triggered_hour=9,
        triggered_weekday=2,
    )
    assert rid > 0
    row = store.conn.execute("SELECT * FROM pipeline_runs WHERE id=?;", (rid,)).fetchone()
    assert row["repo"] == "myorg/myrepo"
    assert row["job_name"] == "build"
    assert row["duration_seconds"] == 42.5


def test_record_audit_event_roundtrip(store: Store) -> None:
    eid = store.record_audit_event(
        platform="github",
        org="myorg",
        event_type="deploy_key.added",
        actor="alice",
        actor_ip="203.0.113.5",
        metadata={"key_title": "ci-deploy"},
        recorded_at="2026-04-20T09:00:00Z",
    )
    row = store.conn.execute("SELECT * FROM audit_events WHERE id=?;", (eid,)).fetchone()
    assert row["actor"] == "alice"
    assert row["metadata"] == '{"key_title": "ci-deploy"}'


def test_default_baseline_path_prefers_local(tmp_path, monkeypatch) -> None:
    # Project-local directory exists → local path wins.
    (tmp_path / ".pipeline-watch").mkdir()
    monkeypatch.chdir(tmp_path)
    p = default_baseline_path()
    assert p == tmp_path / ".pipeline-watch" / "baseline.db"


def test_default_baseline_path_falls_back_to_home(tmp_path, monkeypatch) -> None:
    # No local directory → ~/.pipeline-watch/baseline.db.
    monkeypatch.chdir(tmp_path)
    # Point HOME at a tmp dir so the test doesn't depend on the real user home.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
    # ``Path.home()`` caches via ``os.path.expanduser`` which reads these.
    p = default_baseline_path(cwd=tmp_path)
    assert p.name == "baseline.db"
    assert ".pipeline-watch" in str(p)


def test_transaction_rolls_back_on_exception(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.conn.execute(
                "INSERT INTO package_snapshots "
                "(ecosystem, package, version, maintainers, dependencies, recorded_at) "
                "VALUES ('pypi','x','1.0','[]','{}','2026-04-20');"
            )
            raise RuntimeError("boom")
    count = store.conn.execute(
        "SELECT COUNT(*) FROM package_snapshots;"
    ).fetchone()[0]
    assert count == 0


def test_open_creates_parent_dir(tmp_path) -> None:
    target = tmp_path / "nested" / "baseline.db"
    s = Store.open(target)
    try:
        assert target.exists()
        assert s.schema_version() == 1
    finally:
        s.close()
