"""Optional per-project settings for ``carcara run``: ``.carcara/config.json``.

Keys (all optional; unknown keys are an error):

- ``verifiability_paths``: glob patterns for low-verifiability paths. Runs that
  touch them stop at the approval gate. A configured list replaces
  ``DEFAULT_VERIFIABILITY_PATHS``; ``[]`` disables the trigger.
- ``probes``: name -> URL template for read-only, unauthenticated HTTP GET
  checks, with exactly one ``{arg}`` placeholder outside the host.

Model profiles (``.env`` ``MODEL_*`` files) are separate and not read here.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field

from carcara.installer import PROJECT_CONFIG_REL

# pyproject.toml is not included: only its version/publish sections matter,
# which a path pattern cannot express.
DEFAULT_VERIFIABILITY_PATHS: tuple[str, ...] = (
    ".github/**",
    "**/migrations/**",
    "**/auth/**",
    "**/policy*",
    "**/policy/**",
)
PLACEHOLDER = "{arg}"
_KEYS = ("verifiability_paths", "probes")


class ProjectConfigError(Exception):
    """Unreadable or invalid ``.carcara/config.json``."""


@dataclass(frozen=True)
class ProjectConfig:
    verifiability_paths: list[str] = field(
        default_factory=lambda: list(DEFAULT_VERIFIABILITY_PATHS)
    )
    probes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """JSON-serialisable form; ``parse_project_config`` round-trips it."""
        return {"verifiability_paths": list(self.verifiability_paths), "probes": dict(self.probes)}


def _check_probe(name: object, url: object) -> None:
    if not isinstance(name, str) or not name:
        raise ProjectConfigError(f"probe names must be non-empty strings: {name!r}")
    if not isinstance(url, str):
        raise ProjectConfigError(f"probe {name!r}: URL must be a string")
    if url.count(PLACEHOLDER) != 1:
        raise ProjectConfigError(f"probe {name!r}: URL must contain exactly one {PLACEHOLDER}")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProjectConfigError(f"probe {name!r}: URL must be http(s) with a host")
    if "@" in parts.netloc:
        raise ProjectConfigError(f"probe {name!r}: URL must not contain credentials")
    if PLACEHOLDER in parts.netloc:
        raise ProjectConfigError(f"probe {name!r}: {PLACEHOLDER} must not be in the host")


def parse_project_config(data: object) -> ProjectConfig:
    if not isinstance(data, dict):
        raise ProjectConfigError("config must be a JSON object")
    unknown = sorted(set(data) - set(_KEYS))
    if unknown:
        raise ProjectConfigError(f"unknown config keys: {', '.join(unknown)}")
    paths = data.get("verifiability_paths", list(DEFAULT_VERIFIABILITY_PATHS))
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ProjectConfigError("verifiability_paths must be a list of non-empty strings")
    probes = data.get("probes", {})
    if not isinstance(probes, dict):
        raise ProjectConfigError("probes must be an object of name -> URL template")
    for name, url in probes.items():
        _check_probe(name, url)
    return ProjectConfig(verifiability_paths=list(paths), probes=dict(probes))


def load_project_config(cwd: str) -> ProjectConfig:
    """Read ``<cwd>/.carcara/config.json``; defaults when the file is absent."""
    path = os.path.join(cwd, PROJECT_CONFIG_REL)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return ProjectConfig()
    except (OSError, ValueError) as exc:
        raise ProjectConfigError(f"{PROJECT_CONFIG_REL}: {exc}") from exc
    try:
        return parse_project_config(data)
    except ProjectConfigError as exc:
        raise ProjectConfigError(f"{PROJECT_CONFIG_REL}: {exc}") from exc


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Anchored regex for a posix glob: ``**/``, trailing ``/**``, ``*``, ``?``."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == n:
            out.append("/.*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _under(path: str, root: str) -> str | None:
    """``path`` relative to ``root`` (posix), or None if it is not inside it."""
    if path == root:
        return None
    prefix = root.rstrip(os.sep) + os.sep
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :].replace(os.sep, "/")


def _norm(path: str, cwd: str | None = None) -> str:
    """Strip leading ``./``; with ``cwd``, make absolute paths inside it relative."""
    if cwd and os.path.isabs(path):
        roots = dict.fromkeys((os.path.abspath(cwd), os.path.realpath(cwd)))
        for candidate in dict.fromkeys((os.path.normpath(path), os.path.realpath(path))):
            for root in roots:
                rel = _under(candidate, root)
                if rel is not None:
                    return rel
        return path
    while path.startswith("./"):
        path = path[2:]
    return path


def match_paths(paths: Iterable[str], patterns: Iterable[str], cwd: str | None = None) -> list[str]:
    """Paths (normalized, deduplicated, in order) matching any of ``patterns``.

    With ``cwd``, absolute paths inside it are matched cwd-relative; other
    absolute paths never match relative patterns.
    """
    regexes = [glob_to_regex(_norm(p)) for p in patterns]
    if not regexes:
        return []
    matched: list[str] = []
    for raw in paths:
        path = _norm(raw, cwd)
        if path not in matched and any(r.match(path) for r in regexes):
            matched.append(path)
    return matched
