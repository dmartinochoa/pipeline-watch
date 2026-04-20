"""PyPI JSON API client.

Tiny, focused module: one HTTP call per package, one dataclass out.
The goal is a `PackageSnapshot`-shaped object that the supply-chain
detector can diff against the stored baseline.

Mockability
-----------
Every network call goes through :func:`_fetch_json`, which delegates
to a module-level fetcher that tests can swap via
:func:`set_fetcher`. We deliberately don't pull in ``requests`` — the
standard library's ``urllib.request`` is sufficient and has zero
install surface, which matters for a "zero infrastructure" security
tool.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..baseline.store import PackageSnapshot

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"

# Test hook — overwrite via set_fetcher(). Production default uses urllib.
_Fetcher = Callable[[str, float], bytes]


def _default_fetcher(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "pipeline-watch/0.1 (+https://github.com/dmartinochoa/pipeline-watch)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted URL schema
        return resp.read()


_fetcher: _Fetcher = _default_fetcher


def set_fetcher(fetcher: _Fetcher | None) -> None:
    """Swap the HTTP fetcher. Pass ``None`` to restore the default.

    Tests drop a dict-backed fetcher in here so nothing ever hits the
    real network. Production code never calls this.
    """
    global _fetcher
    _fetcher = fetcher or _default_fetcher


def _fetch_json(url: str, timeout: float = 10.0) -> Any:
    try:
        raw = _fetcher(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise PyPIError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise PyPIError(f"network error fetching {url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PyPIError(f"non-JSON response from {url}: {exc}") from exc


class PyPIError(RuntimeError):
    """Raised for any non-404 failure communicating with PyPI."""


# ── Package metadata parsing ────────────────────────────────────────

# PEP 508 dependency strings look like ``click (>=8.0) ; python_version >= '3.10'``.
# We only need the package name and the constraint fragment for diffing
# baselines, so we split on the environment marker and capture the
# parenthesised constraint (or trailing bare constraint) if present.
_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._\-]+)"
    r"\s*(?:\[[^\]]*\])?"                          # optional extras, ignored
    r"\s*(?P<constraint>\([^)]+\)|[<>=!~][^;]+)?"   # constraint
)

_SDIST_HOOKED_FILES = (
    # Any file we'd want to hash for the install-script signal.
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    # Init files often host postinstall logic disguised as a package-
    # level import (see event-stream compromise).
    "__init__.py",
)


@dataclass
class PyPIRelease:
    """A single release on PyPI."""
    version: str
    upload_time_iso: str
    sdist_url: str | None
    has_install_script: bool
    install_script_hash: str | None
    install_script_size: int = 0
    yanked: bool = False
    yanked_reason: str = ""

    def upload_datetime(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.upload_time_iso.replace("Z", "+00:00"))
        except ValueError:
            return None


@dataclass
class PyPIPackage:
    name: str
    latest_version: str
    maintainers: list[dict]
    releases: list[PyPIRelease]
    dependencies: dict[str, str]
    project_urls: dict[str, str]

    def latest_release(self) -> PyPIRelease | None:
        for r in self.releases:
            if r.version == self.latest_version:
                return r
        return self.releases[0] if self.releases else None

    def source_repo(self) -> str | None:
        """Best-guess GitHub/GitLab repo URL for the package."""
        for key in ("Source", "Homepage", "Source Code", "Repository"):
            url = self.project_urls.get(key, "")
            if "github.com/" in url or "gitlab.com/" in url:
                return url
        return None


def fetch_package(name: str, *, include_install_script_hash: bool = True) -> PyPIPackage | None:
    """Return the PyPI metadata for *name*, or ``None`` if the package is unknown.

    ``include_install_script_hash`` downloads the latest sdist (usually
    ~50–200 KB) and hashes the install-hook files found inside. Tests
    flip it off to keep fixtures small; production callers want it on.
    """
    doc = _fetch_json(PYPI_JSON_URL.format(package=name))
    if doc is None:
        return None

    info = doc.get("info", {}) or {}
    releases_map: dict[str, list[dict]] = doc.get("releases", {}) or {}
    # PyPI exposes maintainers via ``info.author`` + ``info.maintainer``
    # plus top-level ``maintainers`` on the JSON endpoint. A few
    # packages leave the structured fields blank and inline them into
    # ``info.author_email``; we collapse all into a normalized list.
    maintainers = _collect_maintainers(info)
    dependencies = _parse_requires_dist(info.get("requires_dist") or [])
    project_urls = {
        str(k): str(v) for k, v in (info.get("project_urls") or {}).items()
    }

    latest_version = str(info.get("version", ""))

    releases: list[PyPIRelease] = []
    for version, files in releases_map.items():
        sdist = _pick_sdist(files)
        upload_time = _pick_upload_time(files)
        yanked, yanked_reason = _pick_yanked(files)
        has_hook = False
        hook_hash: str | None = None
        hook_size = 0
        if include_install_script_hash and version == latest_version and sdist:
            try:
                has_hook, hook_hash, hook_size = _probe_install_script(sdist["url"])
            except Exception:
                # A failed probe shouldn't break the snapshot — record
                # "we don't know" and move on. A corrupted sdist, 404,
                # or read timeout all deserve graceful degradation.
                has_hook, hook_hash, hook_size = False, None, 0
        releases.append(PyPIRelease(
            version=version,
            upload_time_iso=upload_time,
            sdist_url=sdist.get("url") if sdist else None,
            has_install_script=has_hook,
            install_script_hash=hook_hash,
            install_script_size=hook_size,
            yanked=yanked,
            yanked_reason=yanked_reason,
        ))

    # Sort newest first for the detector's convenience. Tie-break on
    # version string so deterministic ordering holds when upload_time
    # is missing (older releases sometimes omit it).
    releases.sort(key=lambda r: (r.upload_time_iso or "", r.version), reverse=True)

    return PyPIPackage(
        name=str(info.get("name") or name),
        latest_version=latest_version,
        maintainers=maintainers,
        releases=releases,
        dependencies=dependencies,
        project_urls=project_urls,
    )


def snapshot_from_package(
    pkg: PyPIPackage,
    *,
    recorded_at: str,
    manifest_constraint: str = "",
) -> PackageSnapshot:
    """Convert a ``PyPIPackage`` into the ``PackageSnapshot`` row the store holds."""
    latest = pkg.latest_release()
    hour: int | None = None
    weekday: int | None = None
    upload_iso = ""
    if latest:
        dt = latest.upload_datetime()
        upload_iso = latest.upload_time_iso or ""
        if dt:
            hour = dt.hour
            weekday = dt.weekday()
    return PackageSnapshot(
        ecosystem="pypi",
        package=pkg.name,
        version=pkg.latest_version,
        maintainers=pkg.maintainers,
        release_hour=hour,
        release_weekday=weekday,
        has_install_script=bool(latest and latest.has_install_script),
        install_script_hash=latest.install_script_hash if latest else None,
        dependencies=pkg.dependencies,
        recorded_at=recorded_at,
        manifest_constraint=manifest_constraint,
        release_uploaded_at=upload_iso,
        yanked=bool(latest and latest.yanked),
    )


# ── Helpers ─────────────────────────────────────────────────────────


def _collect_maintainers(info: dict) -> list[dict]:
    out: list[dict] = []
    # Structured author/maintainer fields.
    for field_name in ("author", "maintainer"):
        name = (info.get(field_name) or "").strip()
        email = (info.get(f"{field_name}_email") or "").strip()
        if name or email:
            out.append({"name": name, "email": email, "first_seen": ""})
    # Some package uploads carry a ``maintainers`` array on the JSON
    # endpoint (Warehouse adds it for packages with named co-owners).
    for m in info.get("maintainers") or []:
        if isinstance(m, dict):
            out.append({
                "name": str(m.get("name") or m.get("username") or ""),
                "email": str(m.get("email") or ""),
                "first_seen": str(m.get("first_seen") or ""),
            })
    # Deduplicate by (name, email) so the list is stable across runs.
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for m in out:
        key = (m["name"].lower(), m["email"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique


def _parse_requires_dist(requires_dist: list[str]) -> dict[str, str]:
    """Collapse ``requires_dist`` entries into ``{name: constraint}`` map."""
    out: dict[str, str] = {}
    for raw in requires_dist:
        if not isinstance(raw, str):
            continue
        # Drop environment marker — we baseline the package set, not
        # per-marker exclusions. A future iteration can track them.
        primary = raw.split(";", 1)[0].strip()
        m = _REQ_RE.match(primary)
        if not m:
            continue
        name = m.group("name")
        constraint = (m.group("constraint") or "").strip().strip("()")
        out[name] = constraint
    return out


def _pick_sdist(files: list[dict]) -> dict | None:
    """Return the source-distribution entry among a release's files, if any."""
    for f in files:
        if f.get("packagetype") == "sdist":
            return f
    return None


def _pick_upload_time(files: list[dict]) -> str:
    """Return the earliest upload_time across the release's files (ISO8601)."""
    times = [str(f.get("upload_time_iso_8601") or f.get("upload_time") or "")
             for f in files]
    times = [t for t in times if t]
    return min(times) if times else ""


def _pick_yanked(files: list[dict]) -> tuple[bool, str]:
    """Return (is_yanked, reason) — true when any file of the release is yanked."""
    reason = ""
    yanked = False
    for f in files:
        if f.get("yanked"):
            yanked = True
            reason = str(f.get("yanked_reason") or "") or reason
    return yanked, reason


def _probe_install_script(sdist_url: str) -> tuple[bool, str | None, int]:
    """Download *sdist_url* and return ``(has_hook, hash, total_bytes)`` over install-script files.

    "Hook" here means any of setup.py / setup.cfg / pyproject.toml /
    __init__.py — the files an attacker typically modifies when
    weaponising a release (see event-stream's ``flatmap-stream``
    __init__.py add, or the PyPI ``ctx`` typosquat's setup.py change).
    The hash is SHA-256 over the concatenation of these files in a
    stable order; any byte-level change triggers the SC-004 signal.
    """
    raw = _fetcher(sdist_url, 15.0)
    buf = io.BytesIO(raw)
    relevant: list[tuple[str, bytes]] = []
    if sdist_url.endswith(".tar.gz") or sdist_url.endswith(".tgz"):
        with tarfile.open(fileobj=buf, mode="r:gz") as tf:
            for member in tf.getmembers():
                name = member.name.rsplit("/", 1)[-1]
                if name in _SDIST_HOOKED_FILES:
                    try:
                        data = tf.extractfile(member)
                        if data is not None:
                            relevant.append((member.name, data.read()))
                    except KeyError:
                        continue
    elif sdist_url.endswith(".zip") or sdist_url.endswith(".whl"):
        with zipfile.ZipFile(buf) as zf:
            for info in zf.infolist():
                name = info.filename.rsplit("/", 1)[-1]
                if name in _SDIST_HOOKED_FILES:
                    relevant.append((info.filename, zf.read(info)))
    else:
        return False, None, 0

    if not relevant:
        return False, None, 0

    h = hashlib.sha256()
    total = 0
    for path, data in sorted(relevant, key=lambda x: x[0]):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
        total += len(data)
    # "has_install_script" is true when setup.py / setup.cfg /
    # pyproject.toml is present (build hook surface), or when any
    # __init__.py showed up (common trojan vector in event-stream and
    # ctx compromises). A coarse "hooks present" flag is enough to
    # drive the baseline diff.
    has_hook = any(
        name.rsplit("/", 1)[-1] in {"setup.py", "setup.cfg", "pyproject.toml"}
        for name, _ in relevant
    )
    return has_hook, h.hexdigest(), total
