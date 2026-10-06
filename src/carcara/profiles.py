"""Model profiles: ``.env`` files mapping each role to a model.

Parsing mirrors the 0.1.0 bash installer exactly (see ``load_profile``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from carcara.resources import profiles_root

MODEL_KEYS: tuple[str, ...] = (
    "MODEL_MAIN",
    "MODEL_ARCHITECT",
    "MODEL_IMPLEMENTER",
    "MODEL_REVIEWER",
    "MODEL_EXPLORER",
    "MODEL_TEST_RUNNER",
    "MODEL_DOC_WRITER",
)

# Profile recorded by ``carcara install`` in the target project; read by ``carcara run``.
INSTALLED_PROFILE_REL = ".carcara/profile"

_SAFE = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)
_LIST_LINE = re.compile(rb"MODEL_[A-Z_]+=")


class ProfileError(Exception):
    """Invalid or unknown profile; the message matches the bash installer."""


@dataclass(frozen=True)
class Profile:
    name: str
    models: dict[str, str]
    source: str

    def model(self, key: str) -> str:
        return self.models[key]


def _split_lines(data: bytes) -> list[bytes]:
    """Split like ``while read -r line || [ -n "$line" ]``: on ``\\n`` only."""
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _basename_sans_env(path: str) -> str:
    name = os.path.basename(path)
    if name.endswith(".env") and name != ".env":
        name = name[: -len(".env")]
    return name


def parse_profile(data: bytes, source: str) -> dict[str, str]:
    """Parse profile bytes; ``source`` is the path used in error messages."""
    models = dict.fromkeys(MODEL_KEYS, "")
    for raw in _split_lines(data):
        line = raw.decode("utf-8", errors="surrogateescape")
        if line == "" or line.startswith("#"):
            continue
        key = line.split("=", 1)[0]
        # ${line#*=}: everything after the first '=', or the whole line if none.
        value = line.split("=", 1)[1] if "=" in line else line
        if key not in MODEL_KEYS:
            raise ProfileError(f"invalid key in profile {source}: {key}")
        if not _SAFE.fullmatch(value):
            raise ProfileError(f"invalid model for {key} in {source}: '{value}'")
        models[key] = value
    for key in MODEL_KEYS:
        if not models[key]:
            raise ProfileError(f"profile {source} is missing {key}")
    return models


def load_profile(spec: str) -> Profile:
    """Load a profile by file path, or by bare name from the packaged profiles."""
    if os.path.isfile(spec):
        source = spec
        with open(spec, "rb") as fh:
            data = fh.read()
    else:
        if spec == "" or "/" in spec or "." in spec:
            raise ProfileError(f"unknown profile: {spec}")
        res = profiles_root().joinpath(f"{spec}.env")
        if not res.is_file():
            raise ProfileError(f"unknown profile: {spec} (try --list-profiles)")
        source = str(res)
        data = res.read_bytes()
    models = parse_profile(data, source)
    name = _basename_sans_env(source)
    if not _SAFE.fullmatch(name):
        raise ProfileError(f"invalid profile name: {name}")
    return Profile(name=name, models=models, source=source)


def read_installed_profile(root: str) -> str | None:
    """The profile spec recorded by ``carcara install`` in ``root``; None if absent."""
    try:
        with open(os.path.join(root, INSTALLED_PROFILE_REL), encoding="utf-8") as fh:
            spec = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return spec or None


def list_profiles() -> str:
    """Return the ``--list-profiles`` listing (name, then indented MODEL_* lines)."""
    out: list[str] = []
    entries = sorted(
        (e for e in profiles_root().iterdir() if e.name.endswith(".env") and e.is_file()),
        key=lambda e: e.name,
    )
    for entry in entries:
        out.append(_basename_sans_env(entry.name) + "\n")
        for raw in _split_lines(entry.read_bytes()):
            if _LIST_LINE.match(raw):
                out.append("  " + raw.decode("utf-8", errors="surrogateescape") + "\n")
    return "".join(out)
