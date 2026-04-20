"""SQLite baseline store — the single persistence layer for pipeline-watch.

Design notes
------------
* ``sqlite3`` from the standard library only. No ORM, no Alembic, no
  third-party query builder. The schema is small (four tables), the
  query surface is small (a dozen CRUD helpers), and an ORM would
  double the deploy surface without shortening a single call site.
* Baseline path resolution (highest wins):

  1. explicit ``path=`` argument (used by tests against ``:memory:``
     and by callers that shell-out the location)
  2. ``.pipeline-watch/baseline.db`` relative to cwd — the project-
     local baseline, the normal case in a repo
  3. ``~/.pipeline-watch/baseline.db`` — the global fallback, shared
     across all of a user's repos

  The *existence* of ``.pipeline-watch/`` at cwd is what promotes the
  project-local baseline. This matches the ``git``/``pre-commit``
  convention: a directory in the working copy makes the tool
  repo-scoped, no flag needed.

* Migrations are additive and addressed by ``user_version``. Bump
  ``_SCHEMA_VERSION`` and add a stanza to ``_MIGRATIONS``; the opener
  replays only the pending steps. This keeps the "fresh db" and
  "upgrade an old db" paths identical so they can't drift.

* JSON columns are stored as ``TEXT`` and parsed on read. Using SQLite's
  JSON1 extension would be nicer for queries, but JSON1 is a compile-
  time extension that is *usually* present and is the kind of "works
  on my machine" landmine a zero-infrastructure tool has to avoid.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 2

# Each entry runs once, ordered, when the db's ``user_version`` is
# below the entry's index. Appending a migration is safe — never
# rewrite an existing one, since upgraders have already executed it.
_MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS package_snapshots (
        id INTEGER PRIMARY KEY,
        ecosystem TEXT NOT NULL,
        package TEXT NOT NULL,
        version TEXT NOT NULL,
        maintainers TEXT NOT NULL,
        release_hour INTEGER,
        release_weekday INTEGER,
        has_install_script INTEGER NOT NULL DEFAULT 0,
        install_script_hash TEXT,
        dependencies TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pkg_eco_name
        ON package_snapshots(ecosystem, package);
    CREATE INDEX IF NOT EXISTS idx_pkg_eco_name_ver
        ON package_snapshots(ecosystem, package, version);

    CREATE TABLE IF NOT EXISTS pipeline_runs (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        repo TEXT NOT NULL,
        job_name TEXT NOT NULL,
        network_destinations TEXT,
        secrets_accessed TEXT,
        artifact_checksums TEXT,
        duration_seconds REAL,
        config_hash TEXT,
        triggered_at TEXT NOT NULL,
        triggered_hour INTEGER,
        triggered_weekday INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_run_repo_job
        ON pipeline_runs(repo, job_name);

    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,
        org TEXT NOT NULL,
        repo TEXT,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL,
        actor_ip TEXT,
        metadata TEXT,
        recorded_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_audit_org_actor
        ON audit_events(org, actor);

    CREATE TABLE IF NOT EXISTS baseline_stats (
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL,
        metric TEXT NOT NULL,
        mean REAL,
        stddev REAL,
        sample_count INTEGER,
        last_updated TEXT NOT NULL,
        UNIQUE(scope, metric)
    );
    """,
    # v2 — persist manifest constraint and release-upload timestamp
    # on each snapshot. manifest_constraint powers SC-006 across runs;
    # release_uploaded_at powers SC-011 dormant-revival and SC-010
    # version-downgrade comparisons.
    """
    ALTER TABLE package_snapshots ADD COLUMN manifest_constraint TEXT;
    ALTER TABLE package_snapshots ADD COLUMN release_uploaded_at TEXT;
    ALTER TABLE package_snapshots ADD COLUMN yanked INTEGER NOT NULL DEFAULT 0;
    """,
]


@dataclass
class PackageSnapshot:
    """A single observation of a package's state at a point in time."""
    ecosystem: str
    package: str
    version: str
    maintainers: list[dict] = field(default_factory=list)
    release_hour: int | None = None
    release_weekday: int | None = None
    has_install_script: bool = False
    install_script_hash: str | None = None
    dependencies: dict[str, str] = field(default_factory=dict)
    recorded_at: str = ""
    manifest_constraint: str = ""
    release_uploaded_at: str = ""
    yanked: bool = False

    def to_row(self) -> tuple:
        return (
            self.ecosystem,
            self.package,
            self.version,
            json.dumps(self.maintainers),
            self.release_hour,
            self.release_weekday,
            1 if self.has_install_script else 0,
            self.install_script_hash,
            json.dumps(self.dependencies),
            self.recorded_at,
            self.manifest_constraint,
            self.release_uploaded_at,
            1 if self.yanked else 0,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> PackageSnapshot:
        def _get(key: str, default: Any = None) -> Any:
            try:
                return row[key]
            except (IndexError, KeyError):
                return default
        return cls(
            ecosystem=row["ecosystem"],
            package=row["package"],
            version=row["version"],
            maintainers=json.loads(row["maintainers"] or "[]"),
            release_hour=row["release_hour"],
            release_weekday=row["release_weekday"],
            has_install_script=bool(row["has_install_script"]),
            install_script_hash=row["install_script_hash"],
            dependencies=json.loads(row["dependencies"] or "{}"),
            recorded_at=row["recorded_at"],
            manifest_constraint=_get("manifest_constraint") or "",
            release_uploaded_at=_get("release_uploaded_at") or "",
            yanked=bool(_get("yanked") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_baseline_path(cwd: Path | None = None) -> Path:
    """Return the path pipeline-watch should open by default.

    Project-local ``.pipeline-watch/baseline.db`` wins when the
    directory exists (signalling "this repo has its own baseline").
    Otherwise fall back to the global ``~/.pipeline-watch/baseline.db``.
    """
    cwd = cwd or Path.cwd()
    local_dir = cwd / ".pipeline-watch"
    if local_dir.is_dir():
        return local_dir / "baseline.db"
    return Path.home() / ".pipeline-watch" / "baseline.db"


class Store:
    """Thin CRUD layer over the baseline SQLite database.

    Open with ``Store.open(path)`` to resolve the canonical location
    (and create parent directories), or with ``Store(connection)`` when
    you already own the connection (tests pass ``sqlite3.connect(":memory:")``).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        # Foreign keys aren't used yet, but enable them so that adding
        # a cross-table constraint in a future migration doesn't
        # silently become a no-op on existing databases.
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._migrate()

    # ── Lifecycle ────────────────────────────────────────────────────

    @classmethod
    def open(cls, path: str | os.PathLike | None = None) -> Store:
        """Open (and create if needed) the baseline database at *path*.

        Passing ``None`` falls back to :func:`default_baseline_path`.
        Passing ``":memory:"`` is supported for tests.
        """
        if path is None:
            resolved = default_baseline_path()
        else:
            resolved = Path(path) if path != ":memory:" else path  # type: ignore[assignment]
        if resolved != ":memory:":
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(resolved))
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group writes into a single transaction that rolls back on error."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ── Migrations ───────────────────────────────────────────────────

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version;").fetchone()[0]
        if current >= _SCHEMA_VERSION:
            return
        for i, ddl in enumerate(_MIGRATIONS[current:], start=current + 1):
            self.conn.executescript(ddl)
            self.conn.execute(f"PRAGMA user_version = {i};")
        self.conn.commit()

    # ── Package snapshots (supply-chain module) ──────────────────────

    def record_snapshot(self, snap: PackageSnapshot) -> int:
        """Insert a new snapshot row. Returns the new rowid.

        Snapshots are append-only — calling twice for the same
        ``(ecosystem, package, version)`` intentionally records two
        rows so the history (and release-hour distribution) is
        preserved.
        """
        cur = self.conn.execute(
            """
            INSERT INTO package_snapshots
                (ecosystem, package, version, maintainers,
                 release_hour, release_weekday,
                 has_install_script, install_script_hash,
                 dependencies, recorded_at,
                 manifest_constraint, release_uploaded_at, yanked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            snap.to_row(),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def latest_snapshot(
        self, ecosystem: str, package: str
    ) -> PackageSnapshot | None:
        """Return the most recent snapshot for ``ecosystem:package``."""
        row = self.conn.execute(
            """
            SELECT * FROM package_snapshots
             WHERE ecosystem = ? AND package = ?
             ORDER BY datetime(recorded_at) DESC, id DESC
             LIMIT 1;
            """,
            (ecosystem, package),
        ).fetchone()
        return PackageSnapshot.from_row(row) if row else None

    def snapshots_for(
        self, ecosystem: str, package: str, limit: int | None = None
    ) -> list[PackageSnapshot]:
        """Return snapshots for a package, newest first."""
        sql = (
            "SELECT * FROM package_snapshots "
            "WHERE ecosystem = ? AND package = ? "
            "ORDER BY datetime(recorded_at) DESC, id DESC"
        )
        params: tuple[Any, ...] = (ecosystem, package)
        if limit is not None:
            sql += " LIMIT ?"
            params = (ecosystem, package, limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [PackageSnapshot.from_row(r) for r in rows]

    def all_packages(self, ecosystem: str | None = None) -> list[tuple[str, str]]:
        """Return distinct ``(ecosystem, package)`` pairs in the store."""
        if ecosystem:
            rows = self.conn.execute(
                "SELECT DISTINCT ecosystem, package FROM package_snapshots "
                "WHERE ecosystem = ? ORDER BY package;",
                (ecosystem,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT ecosystem, package FROM package_snapshots "
                "ORDER BY ecosystem, package;",
            ).fetchall()
        return [(r["ecosystem"], r["package"]) for r in rows]

    def release_hours(self, ecosystem: str, package: str) -> list[int]:
        """Return every observed ``release_hour`` (non-null) for a package."""
        rows = self.conn.execute(
            "SELECT release_hour FROM package_snapshots "
            "WHERE ecosystem = ? AND package = ? AND release_hour IS NOT NULL;",
            (ecosystem, package),
        ).fetchall()
        return [r["release_hour"] for r in rows]

    # ── Pipeline runs (ci-runtime module — schema only for now) ──────

    def record_run(
        self,
        provider: str,
        repo: str,
        job_name: str,
        *,
        network_destinations: list[str] | None = None,
        secrets_accessed: list[str] | None = None,
        artifact_checksums: dict[str, str] | None = None,
        duration_seconds: float | None = None,
        config_hash: str | None = None,
        triggered_at: str,
        triggered_hour: int | None = None,
        triggered_weekday: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO pipeline_runs
                (provider, repo, job_name,
                 network_destinations, secrets_accessed, artifact_checksums,
                 duration_seconds, config_hash,
                 triggered_at, triggered_hour, triggered_weekday)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                provider, repo, job_name,
                json.dumps(network_destinations or []),
                json.dumps(secrets_accessed or []),
                json.dumps(artifact_checksums or {}),
                duration_seconds, config_hash,
                triggered_at, triggered_hour, triggered_weekday,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # ── VCS audit events (schema only for now) ───────────────────────

    def record_audit_event(
        self,
        platform: str,
        org: str,
        event_type: str,
        actor: str,
        *,
        repo: str | None = None,
        actor_ip: str | None = None,
        metadata: dict | None = None,
        recorded_at: str,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO audit_events
                (platform, org, repo, event_type, actor, actor_ip,
                 metadata, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                platform, org, repo, event_type, actor, actor_ip,
                json.dumps(metadata or {}), recorded_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # ── Baseline statistics ──────────────────────────────────────────

    def upsert_stat(
        self,
        scope: str,
        metric: str,
        *,
        mean: float | None,
        stddev: float | None,
        sample_count: int,
        updated_at: str,
    ) -> None:
        """Insert or replace a row in ``baseline_stats``."""
        self.conn.execute(
            """
            INSERT INTO baseline_stats
                (scope, metric, mean, stddev, sample_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, metric) DO UPDATE SET
                mean = excluded.mean,
                stddev = excluded.stddev,
                sample_count = excluded.sample_count,
                last_updated = excluded.last_updated;
            """,
            (scope, metric, mean, stddev, sample_count, updated_at),
        )
        self.conn.commit()

    def get_stat(self, scope: str, metric: str) -> dict | None:
        row = self.conn.execute(
            "SELECT mean, stddev, sample_count, last_updated "
            "FROM baseline_stats WHERE scope = ? AND metric = ?;",
            (scope, metric),
        ).fetchone()
        if not row:
            return None
        return {
            "mean": row["mean"],
            "stddev": row["stddev"],
            "sample_count": row["sample_count"],
            "last_updated": row["last_updated"],
        }

    def all_stats(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT scope, metric, mean, stddev, sample_count, last_updated "
            "FROM baseline_stats ORDER BY scope, metric;"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Reset ────────────────────────────────────────────────────────

    def reset_scope(self, scope: str) -> int:
        """Delete every trace of *scope* (package, job, or org).

        Scopes are of the form ``package:<name>`` / ``job:<repo>:<job>``
        / ``org:<name>``. The prefix picks the right tables; anything
        unrecognised raises ``ValueError`` so a typo can't silently
        wipe nothing.
        """
        if scope.startswith("package:"):
            name = scope.split(":", 1)[1]
            cur = self.conn.execute(
                "DELETE FROM package_snapshots WHERE package = ?;", (name,),
            )
            self.conn.execute(
                "DELETE FROM baseline_stats WHERE scope = ?;", (scope,),
            )
            self.conn.commit()
            return cur.rowcount
        if scope.startswith("job:"):
            # job:<repo>:<job_name>
            parts = scope.split(":", 2)
            if len(parts) != 3:
                raise ValueError(
                    f"job scope must be 'job:<repo>:<job_name>', got {scope!r}"
                )
            _, repo, job = parts
            cur = self.conn.execute(
                "DELETE FROM pipeline_runs WHERE repo = ? AND job_name = ?;",
                (repo, job),
            )
            self.conn.execute(
                "DELETE FROM baseline_stats WHERE scope = ?;", (scope,),
            )
            self.conn.commit()
            return cur.rowcount
        if scope.startswith("org:"):
            org = scope.split(":", 1)[1]
            cur = self.conn.execute(
                "DELETE FROM audit_events WHERE org = ?;", (org,),
            )
            self.conn.execute(
                "DELETE FROM baseline_stats WHERE scope = ?;", (scope,),
            )
            self.conn.commit()
            return cur.rowcount
        raise ValueError(
            f"scope must start with 'package:', 'job:', or 'org:'; got {scope!r}"
        )

    def schema_version(self) -> int:
        return int(
            self.conn.execute("PRAGMA user_version;").fetchone()[0]
        )
