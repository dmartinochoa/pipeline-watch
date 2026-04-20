"""npm registry client — full metadata fetch + manifest parsing.

Produces a ``PackageSnapshot`` the supply-chain detector can diff
against the stored baseline — analogous to the PyPI provider but
sourcing data from ``registry.npmjs.org``.

Everything routes through a swappable fetcher so tests stay
zero-network.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..baseline.store import PackageSnapshot

NPM_REGISTRY_URL = "https://registry.npmjs.org/{package}"

_Fetcher = Callable[[str, float], bytes]


def _default_fetcher(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "pipeline-watch/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


_fetcher: _Fetcher = _default_fetcher


def set_fetcher(fetcher: _Fetcher | None) -> None:
    global _fetcher
    _fetcher = fetcher or _default_fetcher


class NpmError(RuntimeError):
    """Raised on any non-404 npm registry failure."""


def _fetch_json(url: str, timeout: float = 10.0) -> Any:
    try:
        raw = _fetcher(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise NpmError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise NpmError(f"network error fetching {url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NpmError(f"non-JSON response from {url}: {exc}") from exc


# ── Minimal SC-008 probe (kept for backwards compatibility) ─────────


@dataclass
class NpmPackageInfo:
    """Lightweight registration info used by the cross-ecosystem signal."""
    name: str
    created_iso: str  # ``time.created`` — when the package first appeared

    @property
    def created_date(self) -> str:
        return self.created_iso[:10]


def package_info(name: str) -> NpmPackageInfo | None:
    """Return registration info for *name*, or ``None`` if npm has no such package."""
    doc = _fetch_json(NPM_REGISTRY_URL.format(package=name))
    if doc is None:
        return None
    created = str((doc.get("time") or {}).get("created", ""))
    return NpmPackageInfo(name=str(doc.get("name") or name), created_iso=created)


# ── Full metadata fetch + snapshot conversion ───────────────────────


@dataclass
class NpmRelease:
    version: str
    upload_time_iso: str
    has_install_script: bool
    install_script_hash: str | None
    deprecated: str  # non-empty string means "deprecated on this version"


@dataclass
class NpmPackage:
    name: str
    latest_version: str
    maintainers: list[dict]
    releases: list[NpmRelease]
    dependencies: dict[str, str]
    repository_url: str | None

    def latest_release(self) -> NpmRelease | None:
        for r in self.releases:
            if r.version == self.latest_version:
                return r
        return self.releases[0] if self.releases else None

    def source_repo(self) -> str | None:
        url = self.repository_url or ""
        return url if "github.com/" in url or "gitlab.com/" in url else None


def fetch_package(name: str) -> NpmPackage | None:
    """Return full metadata for *name*, or ``None`` if npm has no such package."""
    doc = _fetch_json(NPM_REGISTRY_URL.format(package=name))
    if doc is None:
        return None

    dist_tags = doc.get("dist-tags") or {}
    latest = str(dist_tags.get("latest") or "")
    versions_map = doc.get("versions") or {}
    latest_view = versions_map.get(latest) or {}

    maintainers = _collect_maintainers(doc, latest_view)
    dependencies = _collect_dependencies(latest_view)
    repository_url = _extract_repository_url(latest_view, doc)

    times = doc.get("time") or {}
    releases: list[NpmRelease] = []
    for ver, view in versions_map.items():
        scripts = (view.get("scripts") or {}) if isinstance(view, dict) else {}
        has_hook, hook_hash = _install_hook_signature(scripts)
        releases.append(NpmRelease(
            version=str(ver),
            upload_time_iso=str(times.get(ver) or ""),
            has_install_script=has_hook,
            install_script_hash=hook_hash,
            deprecated=str(view.get("deprecated") or "") if isinstance(view, dict) else "",
        ))
    releases.sort(key=lambda r: (r.upload_time_iso or "", r.version), reverse=True)

    return NpmPackage(
        name=str(doc.get("name") or name),
        latest_version=latest,
        maintainers=maintainers,
        releases=releases,
        dependencies=dependencies,
        repository_url=repository_url,
    )


def snapshot_from_package(
    pkg: NpmPackage,
    *,
    recorded_at: str,
    manifest_constraint: str = "",
) -> PackageSnapshot:
    latest = pkg.latest_release()
    hour: int | None = None
    weekday: int | None = None
    upload_iso = ""
    if latest:
        upload_iso = latest.upload_time_iso
        dt = _parse_iso(upload_iso)
        if dt:
            hour = dt.hour
            weekday = dt.weekday()
    return PackageSnapshot(
        ecosystem="npm",
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
        yanked=bool(latest and latest.deprecated),
    )


# ── Manifest parsing: package.json ──────────────────────────────────


@dataclass
class NpmManifestEntry:
    name: str
    constraint: str
    source_line: str


def parse_package_json(path: str | Path) -> list[NpmManifestEntry]:
    """Parse ``dependencies`` + ``devDependencies`` out of a package.json."""
    text = Path(path).read_text(encoding="utf-8")
    doc = json.loads(text)
    out: list[NpmManifestEntry] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = doc.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, constraint in deps.items():
            name_s = str(name).strip()
            constraint_s = str(constraint or "").strip()
            if not name_s:
                continue
            out.append(NpmManifestEntry(
                name=name_s,
                constraint=constraint_s,
                source_line=f"{name_s}@{constraint_s}" if constraint_s else name_s,
            ))
    return out


# ── Helpers ─────────────────────────────────────────────────────────


_INSTALL_HOOK_KEYS = ("preinstall", "install", "postinstall")


def _install_hook_signature(scripts: dict) -> tuple[bool, str | None]:
    """Return (has_hook, stable-hash) over install-lifecycle scripts."""
    import hashlib
    present = [(k, str(scripts.get(k) or "")) for k in _INSTALL_HOOK_KEYS if scripts.get(k)]
    if not present:
        return False, None
    h = hashlib.sha256()
    for k, v in sorted(present):
        h.update(k.encode("utf-8"))
        h.update(b"\0")
        h.update(v.encode("utf-8"))
        h.update(b"\0")
    return True, h.hexdigest()


def _collect_maintainers(doc: dict, latest_view: dict) -> list[dict]:
    out: list[dict] = []
    # Top-level maintainers on the registry doc — authoritative for
    # "who can publish". Used to be a string list in ancient packages;
    # normalise to dicts with (name, email).
    for m in doc.get("maintainers") or []:
        out.append(_maintainer_entry(m))
    # Per-version author and maintainers — legitimate publishers
    # sometimes show up here when the registry view lags.
    author = latest_view.get("author") if isinstance(latest_view, dict) else None
    if author:
        out.append(_maintainer_entry(author))
    for m in (latest_view.get("maintainers") or []) if isinstance(latest_view, dict) else []:
        out.append(_maintainer_entry(m))
    # Deduplicate.
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for m in out:
        if not m.get("name") and not m.get("email"):
            continue
        key = (m["name"].lower(), m.get("email", "").lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique


def _maintainer_entry(raw: Any) -> dict:
    if isinstance(raw, dict):
        return {
            "name": str(raw.get("name") or raw.get("username") or "").strip(),
            "email": str(raw.get("email") or "").strip(),
            "first_seen": "",
        }
    if isinstance(raw, str):
        # "Name <email@example.com>" style.
        m = re.match(r"^(.*?)\s*<([^>]+)>$", raw.strip())
        if m:
            return {"name": m.group(1).strip(), "email": m.group(2).strip(), "first_seen": ""}
        return {"name": raw.strip(), "email": "", "first_seen": ""}
    return {"name": "", "email": "", "first_seen": ""}


def _collect_dependencies(view: dict) -> dict[str, str]:
    if not isinstance(view, dict):
        return {}
    deps = view.get("dependencies") or {}
    if not isinstance(deps, dict):
        return {}
    return {str(k): str(v or "") for k, v in deps.items()}


def _extract_repository_url(latest_view: dict, doc: dict) -> str | None:
    candidates: list[Any] = []
    for source in (latest_view, doc):
        if not isinstance(source, dict):
            continue
        repo = source.get("repository")
        if isinstance(repo, dict):
            candidates.append(repo.get("url") or "")
        elif isinstance(repo, str):
            candidates.append(repo)
    for c in candidates:
        url = str(c or "").strip()
        if not url:
            continue
        # npm repositories often carry "git+https://..." prefixes — strip.
        if url.startswith("git+"):
            url = url[4:]
        if url.endswith(".git"):
            url = url[:-4]
        if "github.com/" in url or "gitlab.com/" in url:
            return url
    return None


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
