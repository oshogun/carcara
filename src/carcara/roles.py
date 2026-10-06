"""Agent roles parsed from the packaged ``templates/claude/agents/*.md`` files.

The agent templates are the single source of truth: the installer renders them
into ``.claude/agents`` and ``carcara run`` builds its per-stage options from
them. Frontmatter is a flat ``key: value`` block, so no YAML library is needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from carcara.resources import templates_root

if TYPE_CHECKING:
    from carcara.profiles import Profile


class RoleError(Exception):
    """Malformed agent template."""


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    tools: tuple[str, ...]
    prompt: str

    @property
    def model_key(self) -> str:
        return "MODEL_" + self.name.upper().replace("-", "_")


def parse_frontmatter(text: str, source: str = "<string>") -> tuple[dict[str, str], str]:
    """Split ``---``-delimited ``key: value`` frontmatter from the body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise RoleError(f"{source}: missing frontmatter")
    meta: dict[str, str] = {}
    for idx in range(1, len(lines)):
        line = lines[idx]
        if line.strip() == "---":
            body = "\n".join(lines[idx + 1 :]).lstrip("\n")
            return meta, body
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise RoleError(f"{source}: invalid frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    raise RoleError(f"{source}: unterminated frontmatter")


def parse_role(text: str, source: str = "<string>") -> Role:
    meta, body = parse_frontmatter(text, source)
    for key in ("name", "description", "tools"):
        if not meta.get(key):
            raise RoleError(f"{source}: frontmatter is missing {key}")
    tools = tuple(t.strip() for t in meta["tools"].split(",") if t.strip())
    return Role(name=meta["name"], description=meta["description"], tools=tools, prompt=body)


def load_roles() -> dict[str, Role]:
    """All packaged roles keyed by name, sorted by name."""
    agents = templates_root().joinpath("claude", "agents")
    roles: dict[str, Role] = {}
    for entry in sorted(agents.iterdir(), key=lambda e: e.name):
        if entry.is_file() and entry.name.endswith(".md"):
            role = parse_role(entry.read_text(encoding="utf-8"), entry.name)
            roles[role.name] = role
    return roles


def get_role(name: str) -> Role:
    roles = load_roles()
    if name not in roles:
        raise RoleError(f"unknown role: {name}")
    return roles[name]


def model_for(role: Role | str, profile: Profile | Mapping[str, str]) -> str:
    """Model for ``role`` from a profile (``MODEL_<NAME>``, ``-`` → ``_``)."""
    name = role.name if isinstance(role, Role) else role
    key = "MODEL_" + name.upper().replace("-", "_")
    models: Mapping[str, str] = getattr(profile, "models", profile)
    if key not in models:
        raise RoleError(f"profile has no {key}")
    return models[key]


def to_agent_definition(role: Role, profile: Profile | Mapping[str, str]) -> Any:
    """Build a ``claude_agent_sdk.AgentDefinition`` (SDK imported lazily)."""
    from claude_agent_sdk import AgentDefinition

    return AgentDefinition(
        description=role.description,
        prompt=role.prompt,
        tools=list(role.tools),
        model=model_for(role, profile),
    )
