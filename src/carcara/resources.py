"""Access to packaged data (templates and profiles) and template rendering."""

from __future__ import annotations

from collections.abc import Iterator
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from carcara.profiles import Profile


def data_root() -> Traversable:
    return files("carcara.data")


def templates_root() -> Traversable:
    return data_root().joinpath("templates")


def profiles_root() -> Traversable:
    return data_root().joinpath("profiles")


def _walk(node: Traversable, prefix: str) -> Iterator[tuple[str, Traversable]]:
    for child in node.iterdir():
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            yield from _walk(child, rel + "/")
        elif child.is_file():
            yield rel, child


def iter_claude_templates() -> list[tuple[str, Traversable]]:
    """Files under ``templates/claude`` as (relative path, resource), byte-sorted
    like ``find | LC_ALL=C sort``."""
    return sorted(
        _walk(templates_root().joinpath("claude"), ""),
        key=lambda item: item[0].encode("utf-8", errors="surrogateescape"),
    )


def claude_md_template() -> Traversable:
    return templates_root().joinpath("CLAUDE.md")


def render(text: str, profile: Profile) -> str:
    """Replace ``{{MODEL_*}}`` and ``{{PROFILE}}`` placeholders, in bash order."""
    for key, value in profile.models.items():
        text = text.replace("{{" + key + "}}", value)
    return text.replace("{{PROFILE}}", profile.name)
