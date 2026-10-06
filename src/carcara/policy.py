"""Per-role tool policy for ``carcara run``.

``decide`` is the pure, authoritative rule set. The SDK adapters wire it in:

- ``make_pre_tool_use_hook`` -- a PreToolUse hook. Hooks run first and a hook
  deny applies in every permission mode, so this is the real gate.
- ``disallowed_tools_for`` -- strips every non-role tool from the context.
- ``permission_mode_for`` -- never ``bypassPermissions``; always explicit.
- ``make_can_use_tool`` -- secondary callback (only reached for calls not
  auto-approved), allowing exactly what ``decide`` allows.

Secret paths (``.env``, ``.env.*``, ``secrets/**``) are matched
case-insensitively, both as written and after symlink resolution.

Read/Grep/Glob (all roles) and read-only Bash path arguments must resolve --
after ``~`` expansion, normalisation and symlink resolution -- inside ``cwd``.

Accepted limitations:

- The Grep tool and recursive search commands (``rg``, ``find``) may still
  surface secret file contents or names when searching a directory that
  contains them; only explicit paths/globs naming ``.env``/``secrets`` are
  blocked, so pattern-based discovery (e.g. ``find . -name '*nv'``) is not.
- git pathspec globs (``git log -- ':(glob)**'``) are not secret-checked;
  git only reads tracked content, which should not include secrets.
- test-runner and implementer have unrestricted Bash (no path confinement or
  secret checks).

Read-only Bash (explorer/reviewer) is a strict allowlist: the command prefix
must be in ``READ_ONLY_PREFIXES`` (git status/diff/log/show, ls, rg, grep,
find, cat, wc, head, tail), no shell metacharacters are allowed, and every
option must appear in that command's ``FLAG_SPECS`` entry (``_FIND_*`` for
find predicates). Unknown options, abbreviations and anything that executes,
writes or widens the search (grep -r, rg --pre/--hidden/-u, find -exec/-delete,
git --output/--ext-diff) are denied.

Write tools (all roles) may only touch paths that resolve -- after ``~``
expansion, normalisation and symlink resolution -- inside ``cwd`` and outside
any ``.git``, ``.carcara`` or ``.claude`` directory.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from carcara.roles import Role, get_role

WRITER_ROLES = frozenset({"implementer", "doc-writer"})
UNRESTRICTED_BASH_ROLES = frozenset({"implementer", "test-runner"})
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
ALWAYS_DENIED = frozenset({"Task", "Agent"})
# With ``output_format`` json_schema the Claude Code CLI delivers structured
# output via a tool call with this exact name; denying it makes every stage
# loop until ``error_max_turns``. Always allowed, never disallowed.
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"

# Built-in Claude Code tools; anything here that a role lacks is disallowed.
KNOWN_TOOLS: tuple[str, ...] = (
    "Agent",
    "Bash",
    "BashOutput",
    "Edit",
    "ExitPlanMode",
    "Glob",
    "Grep",
    "KillBash",
    "KillShell",
    "ListMcpResourcesTool",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "ReadMcpResourceTool",
    "SlashCommand",
    "Skill",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)

# Read-only Bash allowlist (command prefix tokens) for non-writer roles.
READ_ONLY_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("ls",),
    ("rg",),
    ("grep",),
    ("find",),
    ("cat",),
    ("wc",),
    ("head",),
    ("tail",),
)


@dataclass(frozen=True)
class FlagSpec:
    """Exact option allowlist for one read-only command.

    ``short``/``short_value``: single-letter flags without/with a value (value
    attached as ``-A3`` or as the next token; clusters like ``-rn`` are split).
    ``long``/``long_value``: ``--name`` flags without/with a value (``--x=v``
    or ``--x v``); ``long_optional`` may carry ``=value``. A separate value
    token may not start with ``-``: when a tool treats the value as optional
    (``git --pretty``, ``-U``) it would otherwise parse it as an unchecked
    flag. ``numeric`` allows ``-N`` (e.g. ``git log -3``). ``glob_values``
    are checked for secret mentions. Anything else -- unknown options, GNU-style abbreviations such
    as ``--recur`` -- is denied.
    """

    short: str = ""
    short_value: str = ""
    long: frozenset[str] = frozenset()
    long_value: frozenset[str] = frozenset()
    long_optional: frozenset[str] = frozenset()
    numeric: bool = False
    glob_values: frozenset[str] = frozenset()


_GIT_SPEC = FlagSpec(
    short="psbw",
    short_value="nU",
    long=frozenset(
        {
            "--name-only",
            "--name-status",
            "--oneline",
            "--patch",
            "--no-patch",
            "--cached",
            "--staged",
            "--no-color",
            "--shortstat",
            "--numstat",
            "--summary",
            "--short",
            "--branch",
            "--porcelain",
            "--graph",
            "--decorate",
            "--no-decorate",
            "--all",
            "--reverse",
            "--first-parent",
            "--no-merges",
            "--merges",
            "--abbrev-commit",
            "--ignore-all-space",
            "--no-ext-diff",
            "--no-textconv",
        }
    ),
    long_value=frozenset(
        {
            "--max-count",
            "--format",
            "--since",
            "--until",
            "--author",
            "--grep",
            "--unified",
        }
    ),
    long_optional=frozenset({"--stat", "--pretty"}),
    numeric=True,
)

# Read-only flag allowlists. Notably absent: grep -r/-R/-d/-f/--include
# (recursive grep can surface secrets; -f reads a pattern file), rg --pre,
# --hidden, -u, --no-ignore, -z, --hostname-bin (all execute or widen the
# search), find -exec/-ok/-delete/-fprint*/-fls, and git --output,
# --ext-diff, --textconv, --no-index, -c, -C.
FLAG_SPECS: dict[str, FlagSpec] = {
    "grep": FlagSpec(
        short="nilLcEFwvHhoqsx",
        short_value="ABCem",
        long=frozenset(
            {
                "--line-number",
                "--ignore-case",
                "--files-with-matches",
                "--files-without-match",
                "--count",
                "--extended-regexp",
                "--fixed-strings",
                "--word-regexp",
                "--invert-match",
                "--with-filename",
                "--no-filename",
                "--only-matching",
                "--quiet",
                "--no-messages",
                "--line-regexp",
            }
        ),
        long_value=frozenset(
            {
                "--after-context",
                "--before-context",
                "--context",
                "--regexp",
                "--max-count",
            }
        ),
        long_optional=frozenset({"--color", "--colour"}),
        numeric=True,
    ),
    "rg": FlagSpec(
        short="nilcFwvHoqsSNx",
        short_value="ABCetTgm",
        long=frozenset(
            {
                "--line-number",
                "--no-line-number",
                "--ignore-case",
                "--case-sensitive",
                "--smart-case",
                "--files-with-matches",
                "--count",
                "--fixed-strings",
                "--word-regexp",
                "--invert-match",
                "--with-filename",
                "--no-filename",
                "--only-matching",
                "--quiet",
                "--line-regexp",
                "--files",
                "--heading",
                "--no-heading",
            }
        ),
        long_value=frozenset(
            {
                "--after-context",
                "--before-context",
                "--context",
                "--regexp",
                "--type",
                "--type-not",
                "--glob",
                "--iglob",
                "--max-count",
                "--max-depth",
                "--color",
            }
        ),
        glob_values=frozenset({"-g", "--glob", "--iglob"}),
    ),
    "git": _GIT_SPEC,
    "ls": FlagSpec(short="laAh1RtrSdFp"),
    "cat": FlagSpec(short="n"),
    "wc": FlagSpec(short="lwcmL"),
    "head": FlagSpec(short="q", short_value="nc", numeric=True),
    "tail": FlagSpec(short="q", short_value="nc", numeric=True),
}

# find takes single-dash predicates rather than getopt options.
_FIND_FLAGS = frozenset({"-print", "-print0", "-not", "-o", "-or", "-a", "-and", "-empty"})
_FIND_VALUE = frozenset(
    {
        "-name",
        "-iname",
        "-type",
        "-path",
        "-ipath",
        "-maxdepth",
        "-mindepth",
        "-newer",
        "-size",
        "-mtime",
        "-mmin",
    }
)
_FIND_GLOBS = frozenset({"-name", "-iname", "-path", "-ipath"})

# Unquoted characters that enable chaining, redirection, substitution or globbing.
_UNQUOTED_META = set(";&|<>()`$\\\n\r*?[]{}!#")
# Inside double quotes these are still interpreted by the shell.
_DQUOTE_META = set("`$\\!")


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


ALLOW = Decision(True, "")


def _resolve_role(role: Role | str) -> Role | None:
    if isinstance(role, Role):
        return role
    try:
        return get_role(role)
    except Exception:
        return None


def _has_secret_part(path: str) -> bool:
    norm = posixpath.normpath(path.replace("\\", "/"))
    for part in norm.lower().split("/"):
        if part == ".env" or part.startswith(".env.") or part == "secrets":
            return True
    return False


def _resolve(path: str, cwd: str | None) -> tuple[str, str, str, str]:
    """(root_lex, root_real, lexical, real) for ``path`` against ``cwd``."""
    root_lex = os.path.abspath(cwd or os.getcwd())
    root_real = os.path.realpath(root_lex)
    lexical = os.path.normpath(os.path.join(root_lex, os.path.expanduser(path)))
    return root_lex, root_real, lexical, os.path.realpath(lexical)


def is_secret_path(path: str, cwd: str | None = None) -> bool:
    """True for ``.env``, ``.env.*`` and ``secrets/**`` (any depth, any case).

    Checked as written and after resolving symlinks against ``cwd`` (or the
    process cwd), so ``link -> .env`` is caught too.
    """
    if not path:
        return False
    written = path
    if cwd and os.path.isabs(path):
        try:
            written = os.path.relpath(path, cwd)
        except ValueError:
            pass
    if _has_secret_part(written):
        return True
    _, root_real, _, real = _resolve(path, cwd)
    if _is_within(real, root_real):
        real = os.path.relpath(real, root_real)
    return _has_secret_part(real)


_SECRET_SAMPLES = (".env", ".env.local", "secrets")


def _mentions_secret(text: str) -> bool:
    """Loose check for glob patterns: any segment that names or could match a secret.

    A segment is denied if it mentions ``.env``/``secrets`` literally, or if it
    starts with ``.`` or contains ``env``/``secret`` and fnmatch-matches one of
    ``.env``, ``.env.local``, ``secrets`` (so ``.en[v]``, ``.en?``, ``*env``,
    ``secret*`` and ``.*`` are caught). Bare ``*``/``**`` stay allowed: they
    are how Glob works, and a later Read of a secret is still denied by path.
    Residual gaps: segments like ``*nv`` or ``s*`` (no leading dot, no
    ``env``/``secret``) and brace alternation (fnmatch has none) can still
    *list* secret file names; their contents remain unreadable.
    """
    if is_secret_path(text):
        return True
    for part in text.replace("\\", "/").lower().split("/"):
        if ".env" in part or part.startswith("secrets") or part.endswith("secrets"):
            return True
        if part in ("*", "**"):
            continue
        if (part.startswith(".") or "env" in part or "secret" in part) and any(
            fnmatch.fnmatchcase(sample, part) for sample in _SECRET_SAMPLES
        ):
            return True
    return False


def _outside_cwd(path: str, cwd: str | None) -> bool:
    """True if ``path`` resolves (``~``, ``..``, symlinks) outside ``cwd``."""
    _, root_real, _, real = _resolve(path, cwd)
    return not _is_within(real, root_real)


_GLOB_CHARS = set("*?[{")


def _glob_outside_cwd(pattern: str, cwd: str | None) -> bool:
    """Confinement for glob patterns: check the literal prefix; no ``..`` after it."""
    parts = pattern.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if _GLOB_CHARS & set(part):
            if ".." in parts[i:]:
                return True
            prefix = "/".join(parts[:i]) or ("/" if pattern.startswith("/") else ".")
            return _outside_cwd(prefix, cwd)
    return _outside_cwd(pattern, cwd)


def _unsafe_shell(command: str) -> str | None:
    """Return the offending character if ``command`` uses shell features."""
    quote: str | None = None
    prev = " "
    for ch in command:
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == '"':
                quote = None
            elif ch in _DQUOTE_META:
                return ch
        elif ch in ("'", '"'):
            quote = ch
        elif ch in _UNQUOTED_META:
            return ch
        elif ch == "~" and (prev.isspace() or prev in "=:"):
            return ch  # tilde expansion only happens at the start of a word
        prev = ch
    if quote is not None:
        return "unterminated quote"
    return None


def _flag_denied(tok: str) -> Decision:
    return _deny(f"argument not allowed for read-only roles: {tok}")


def _check_flags(spec: FlagSpec, args: list[str]) -> Decision | tuple[list[str], list[str]]:
    """Validate ``args`` against ``spec``; return (glob values, positionals) or a denial."""
    globs: list[str] = []
    positionals: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok == "--":
            positionals.extend(args[i:])  # everything after is positional
            break
        if tok == "-" or not tok.startswith("-"):
            positionals.append(tok)
            continue
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name in spec.long_value:
                if not eq:
                    if i >= len(args) or args[i].startswith("-"):
                        return _deny(f"{name} needs a value (attach dash values with =)")
                    value = args[i]
                    i += 1
                if name in spec.glob_values:
                    globs.append(value)
            elif name in spec.long_optional or (name in spec.long and not eq):
                pass
            else:
                return _flag_denied(tok)
            continue
        if spec.numeric and tok[1:].isdigit():
            continue
        for pos, ch in enumerate(tok[1:], 1):
            if ch in spec.short:
                continue
            if ch in spec.short_value:
                value = tok[pos + 1 :]
                if not value:
                    if i >= len(args) or args[i].startswith("-"):
                        return _deny(f"-{ch} needs a value (attach dash values directly)")
                    value = args[i]
                    i += 1
                if f"-{ch}" in spec.glob_values:
                    globs.append(value)
                break
            return _flag_denied(tok)
    return globs, positionals


def _check_find_args(args: list[str]) -> Decision | tuple[list[str], list[str]]:
    """Validate find args; return (glob values, path operands) or a denial."""
    globs: list[str] = []
    paths: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok in _FIND_FLAGS:
            continue
        if not tok.startswith("-"):
            paths.append(tok)  # starting points
            continue
        if tok not in _FIND_VALUE:
            return _flag_denied(tok)
        if i >= len(args):
            return _deny(f"missing value for {tok}")
        if tok in _FIND_GLOBS:
            globs.append(args[i])
        elif tok == "-newer":
            paths.append(args[i])
        i += 1
    return globs, paths


def _has_pattern_operand(cmd: str, args: list[str]) -> bool:
    """grep/rg take the pattern as first positional unless -e/--regexp/--files."""
    if cmd not in ("grep", "rg"):
        return False
    for tok in args:
        if tok == "--":
            break
        if tok in ("--files", "--regexp") or tok.startswith("--regexp="):
            return False
        if tok.startswith("-") and not tok.startswith("--"):
            spec = FLAG_SPECS[cmd]
            for ch in tok[1:]:
                if ch == "e":
                    return False
                if ch in spec.short_value:
                    break
    return True


def _check_read_only_bash(command: str, cwd: str | None = None) -> Decision:
    bad = _unsafe_shell(command)
    if bad is not None:
        return _deny(f"shell metacharacter not allowed for read-only roles: {bad!r}")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return _deny(f"unparseable command: {exc}")
    if not tokens:
        return _deny("empty command")
    if not any(tuple(tokens[: len(p)]) == p for p in READ_ONLY_PREFIXES):
        return _deny(f"command not in read-only allowlist: {tokens[0]}")
    args = tokens[2:] if tokens[0] == "git" else tokens[1:]
    if tokens[0] == "find":
        result = _check_find_args(args)
    else:
        result = _check_flags(FLAG_SPECS[tokens[0]], args)
    if isinstance(result, Decision):
        return result
    globs, paths = result
    for glob in globs:
        if _mentions_secret(glob):
            return _deny(f"access to secret files is denied: {glob}")
    for tok in tokens[1:]:
        pieces = re.split(r"[:=]", tok)
        if any(is_secret_path(p, cwd) for p in pieces) or (
            tok.startswith("-") and ".env" in tok.lower()
        ):
            return _deny(f"access to secret files is denied: {tok}")
    if _has_pattern_operand(tokens[0], args):
        paths = paths[1:]
    for path in paths:
        if path != "-" and _outside_cwd(path, cwd):
            return _deny(f"path outside the working directory is denied: {path}")
    return ALLOW


def _path_args(tool_name: str, tool_input: Mapping[str, Any]) -> list[tuple[str, bool]]:
    """(value, is_glob) pairs naming files the tool will touch."""
    out: list[tuple[str, bool]] = []
    for key in ("file_path", "notebook_path", "path"):
        if tool_input.get(key) is not None:
            out.append((tool_input[key], False))
    if tool_name == "Glob" and "pattern" in tool_input:
        pattern = tool_input["pattern"]
        out.append((pattern, True))
        # Glob resolves the pattern relative to ``path``: confine the joined
        # form too, so ``path=src`` + ``out/*`` with ``src/out -> ../..`` is caught.
        base = tool_input.get("path")
        if isinstance(base, str) and base and isinstance(pattern, str):
            out.append((posixpath.join(base.replace("\\", "/"), pattern), True))
    if tool_name == "Grep" and "glob" in tool_input:
        out.append((tool_input["glob"], True))
    if tool_name == "MultiEdit":
        for edit in tool_input.get("edits") or ():
            if isinstance(edit, Mapping) and "file_path" in edit:
                out.append((edit["file_path"], False))
    return out


_PROTECTED_DIRS = frozenset({".git", ".carcara", ".claude"})


def _write_path_problem(path: str, cwd: str | None) -> str | None:
    """Why a write to ``path`` is refused, or None if it stays inside ``cwd``.

    The path is ``~``-expanded, made absolute against ``cwd`` and resolved
    with ``realpath`` (which follows symlinks of existing parents), so
    ``../x``, ``/etc/x`` and symlinked directories pointing elsewhere are all
    caught. Any ``.git``, ``.carcara`` or ``.claude`` component (any case) is
    refused too: ``.claude/settings.json`` hooks would run in the user's next
    session.
    """
    if not path:
        return "requires a path"
    root_lex, root_real, lexical, real = _resolve(path, cwd)
    if not _is_within(real, root_real):
        return "outside the working directory is denied"
    rels = [os.path.relpath(real, root_real)]
    if _is_within(lexical, root_lex):
        rels.append(os.path.relpath(lexical, root_lex))
    for rel in rels:
        if any(part.lower() in _PROTECTED_DIRS for part in rel.split(os.sep)):
            return "into .git/.carcara/.claude is denied"
    return None


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def decide(
    role: Role | str, tool_name: str, tool_input: Mapping[str, Any] | None, cwd: str | None
) -> Decision:
    """Pure policy decision for one tool call by ``role``."""
    if tool_name == STRUCTURED_OUTPUT_TOOL:
        return ALLOW
    resolved = _resolve_role(role)
    if resolved is None:
        return _deny(f"unknown role: {role}")
    name = resolved.name
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    if tool_name in ALWAYS_DENIED:
        return _deny(f"{tool_name} is disabled in carcara run")
    if tool_name not in resolved.tools:
        return _deny(f"{tool_name} is not allowed for role {name}")
    if tool_name in WRITE_TOOLS and name not in WRITER_ROLES:
        return _deny(f"{tool_name} is not allowed for read-only role {name}")
    for value, is_glob in _path_args(tool_name, tool_input):
        if not isinstance(value, str):
            return _deny(f"invalid path argument for {tool_name}")
        if is_secret_path(value, cwd) or (is_glob and _mentions_secret(value)):
            return _deny(f"access to secret files is denied: {value}")
        if tool_name in WRITE_TOOLS:
            problem = _write_path_problem(value, cwd)
            if problem:
                return _deny(f"{tool_name} {problem}: {value}")
        elif tool_name in READ_TOOLS and not (is_glob and tool_name == "Grep"):
            outside = _glob_outside_cwd(value, cwd) if is_glob else _outside_cwd(value, cwd)
            if outside:
                return _deny(f"{tool_name} outside the working directory is denied: {value}")
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return _deny("Bash requires a string command")
        if name not in UNRESTRICTED_BASH_ROLES:
            return _check_read_only_bash(command, cwd)
    return ALLOW


def disallowed_tools_for(role: Role) -> list[str]:
    """Known built-in tools the role does not have (always incl. Task/Agent)."""
    return [
        t
        for t in KNOWN_TOOLS
        if (t not in role.tools or t in ALWAYS_DENIED) and t != STRUCTURED_OUTPUT_TOOL
    ]


def allowed_tools_for(role: Role) -> list[str]:
    """The role's tools (minus Task/Agent) plus ``StructuredOutput``."""
    tools = [t for t in role.tools if t not in ALWAYS_DENIED]
    if STRUCTURED_OUTPUT_TOOL not in tools:
        tools.append(STRUCTURED_OUTPUT_TOOL)
    return tools


def permission_mode_for(role: Role | str) -> str:
    name = role.name if isinstance(role, Role) else role
    return "acceptEdits" if name in WRITER_ROLES else "dontAsk"


HookCallback = Callable[[dict[str, Any], "str | None", Any], Awaitable[dict[str, Any]]]


def make_pre_tool_use_hook(role: Role | str, cwd: str | None) -> HookCallback:
    """PreToolUse hook: deny per ``decide``; ``{}`` (pass through) otherwise."""

    async def hook(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        decision = decide(role, input_data.get("tool_name", ""), input_data.get("tool_input"), cwd)
        if decision.allow:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"carcara policy: {decision.reason}",
            }
        }

    return hook


def make_can_use_tool(
    role: Role | str, cwd: str | None
) -> Callable[[str, dict[str, Any], Any], Awaitable[Any]]:
    """Secondary ``can_use_tool`` callback mirroring ``decide`` (SDK imported lazily)."""

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        decision = decide(role, tool_name, tool_input, cwd)
        if decision.allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"carcara policy: {decision.reason}")

    return can_use_tool
