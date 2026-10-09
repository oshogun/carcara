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
- test-runner and implementer Bash is guarded only by a best-effort deny-list
  (``_check_unrestricted_bash``: git push, git reset --hard, git clean -f,
  fetch-and-exec, shell access to secret paths, shell writes -- including
  ``sed -i``/``perl -i``/``ruby -i`` -- outside ``cwd`` or into
  ``.git``/``.carcara``/``.claude``, and ``-c``/``-e``/``eval`` code strings
  naming those directories). Variables, paths built at runtime, base64,
  ``cd``, aliases, heredoc/stdin programs, scripts written into the repo and
  then run, ``awk -i inplace``, ex/vi/ed and ``dd of=`` all bypass it; reads
  outside ``cwd`` are allowed. Wrapper options that take a value (``sudo -u``,
  ``nice -n``, ``env -u``/``-C``/``-S``, ...) are skipped via
  ``_WRAPPER_VALUE_OPTS``; options missing from that table can still hide the
  wrapped command. The only opt-out is ``carcara run --unrestricted-bash``,
  resolved by the orchestrator before the run and passed to ``decide`` as
  ``unrestricted_bash``, so a stage cannot turn it on. There is no
  per-project test-command allowlist yet.

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
# Best-effort Bash deny-list for UNRESTRICTED_BASH_ROLES (see _check_unrestricted_bash).
SHELL_EXEC_TARGETS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "python", "python3", "perl", "ruby", "node"}
    | {"eval", "source", "."}
)
NETWORK_FETCHERS = frozenset({"curl", "wget"})
SHELL_WRITE_COMMANDS = frozenset(
    {"rm", "mv", "cp", "ln", "mkdir", "touch", "chmod", "chown", "truncate", "install", "rmdir"}
)
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


_SEGMENT_PUNCT = "();<>|&\n"
_SEGMENT_PUNCT_SET = frozenset(_SEGMENT_PUNCT)
# ``2>``/``1>>``: drop the fd number so it is not mistaken for an argument.
_FD_PREFIX = re.compile(r"(?<![\w$-])\d+(?=[<>])")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1[^\n]*\n")


def _strip_heredocs(command: str) -> str:
    """Drop here-document bodies so their text is not parsed as commands."""
    out: list[str] = []
    pos = 0
    while (m := _HEREDOC.search(command, pos)) is not None:
        out.append(command[pos : m.end()])
        delim = re.compile(rf"^\t*{re.escape(m.group(2))}[ \t]*$", re.MULTILINE)
        end = delim.search(command, m.end())
        if end is None:
            return "".join(out)  # unterminated: the rest is body
        pos = end.end()
    out.append(command[pos:])
    return "".join(out)


def _shell_segments(command: str) -> list[tuple[str, list[str]]] | None:
    """Split ``command`` into simple commands as (preceding operator, tokens).

    Quote-aware via shlex: ``;``, ``&&``, ``||``, ``|``, ``|&``, ``&``,
    newlines and subshell parentheses separate segments (the operator is
    ``"|"`` for any pipe). Redirection operators stay in the tokens.
    Here-document bodies are skipped. Returns None when unparseable
    (e.g. unbalanced quotes).
    """
    text = _strip_heredocs(command.replace("\\\n", " "))
    lex = shlex.shlex(_FD_PREFIX.sub("", text), posix=True, punctuation_chars=_SEGMENT_PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[tuple[str, list[str]]] = []
    op = ""
    current: list[str] = []
    for tok in tokens:
        is_punct = bool(tok) and set(tok) <= _SEGMENT_PUNCT_SET
        if not is_punct or (("<" in tok or ">" in tok) and "(" not in tok and ")" not in tok):
            current.append(tok)
            continue
        sep = "|" if "|" in tok and "||" not in tok else tok
        if current:
            segments.append((op, current))
            current = []
            op = sep
        elif sep != "(":
            op = sep
    if current:
        segments.append((op, current))
    return segments


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


_WRAPPERS = frozenset(
    {"sudo", "env", "command", "builtin", "exec", "xargs", "nohup", "nice", "time", "timeout"}
)
# Wrapper options whose value is the next argv token (attached forms need no skip).
_WRAPPER_VALUE_OPTS: dict[str, frozenset[str]] = {
    "sudo": frozenset(
        {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"}
        | {"--user", "--group", "--host", "--prompt", "--chdir", "--close-from"}
        | {"--role", "--type", "--other-user", "--command-timeout"}
    ),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "xargs": frozenset(
        {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"}
        | {"--max-args", "--max-procs", "--delimiter", "--arg-file", "--max-chars"}
        | {"--eof", "--replace"}
    ),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
}
_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
_GIT_VALUE_OPTS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
_DEV_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
_FETCH_SUBST = re.compile(r"(?:[<$]\(|`)\s*(?:curl|wget)\b")
_DEST_COMMANDS = frozenset({"cp", "mv", "ln", "install"})


def _strip_wrappers(argv: list[str]) -> list[str]:
    """Drop leading ``VAR=x`` assignments and wrappers like ``sudo``/``env``/``xargs``.

    Values of ``_WRAPPER_VALUE_OPTS`` are skipped too; ``env -S``/``--split-string``
    strings are split and checked as the wrapped command.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if _ASSIGNMENT.match(tok):
            i += 1
            continue
        base = posixpath.basename(tok)
        if base not in _WRAPPERS:
            break
        value_opts = _WRAPPER_VALUE_OPTS.get(base, frozenset())
        i += 1
        while i < len(argv) and (argv[i].startswith("-") or _ASSIGNMENT.match(argv[i])):
            opt = argv[i]
            i += 1
            split = None
            if base == "env" and opt in ("-S", "--split-string") and i < len(argv):
                split, i = argv[i], i + 1
            elif base == "env" and opt.startswith("--split-string="):
                split = opt.partition("=")[2]
            elif base == "env" and opt.startswith("-S") and len(opt) > 2:
                split = opt[2:]
            elif opt in value_opts:
                i += 1
            if split is not None:
                try:
                    words = shlex.split(split)
                except ValueError:
                    words = split.split()
                argv, i = ["env", *words, *argv[i:]], 0
                break
        else:
            if base == "timeout" and i < len(argv):
                i += 1  # duration
    return argv[i:]


def _split_redirects(tokens: list[str]) -> tuple[list[str], list[str]]:
    """(argv without redirections, files written by ``>``-style redirections)."""
    argv: list[str] = []
    targets: list[str] = []
    it = iter(tokens)
    for tok in it:
        if not (tok and set(tok) <= _SEGMENT_PUNCT_SET):
            argv.append(tok)
            continue
        target = next(it, "")
        if ">" not in tok or (tok.endswith("&") and (target.isdigit() or target == "-")):
            continue  # input, here-string or fd duplication
        if target not in _DEV_SINKS:
            targets.append(target)
    return argv, targets


def _is_exec_target(cmd: str) -> bool:
    base = posixpath.basename(cmd)
    return base in SHELL_EXEC_TARGETS or base.startswith("python")


def _git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    """Skip git global options; return (subcommand, its arguments)."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in _GIT_VALUE_OPTS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return tok, args[i + 1 :]
    return "", []


def _write_operands(cmd: str, args: list[str]) -> list[str]:
    """Paths a SHELL_WRITE_COMMANDS invocation modifies (destination only for cp/mv/ln/install)."""
    operands: list[str] = []
    dests: list[str] = []
    end_opts = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if not end_opts and tok == "--":
            end_opts = True
        elif not end_opts and tok.startswith("-") and tok != "-":
            if cmd not in _DEST_COMMANDS:
                continue
            if tok in ("-t", "--target-directory") and i < len(args):
                dests.append(args[i])
                i += 1
            elif tok.startswith("--target-directory="):
                dests.append(tok.partition("=")[2])
            elif tok.startswith("-t") and not tok.startswith("--"):
                dests.append(tok[2:])
        else:
            operands.append(tok)
    if cmd not in _DEST_COMMANDS:
        return operands
    if dests:
        return dests
    if cmd == "ln" and len(operands) == 1:
        return ["."]  # link created in the current directory
    return operands[-1:]


_SED_COMMANDS = frozenset({"sed", "gsed"})
_SED_VALUE_LONG = frozenset({"--expression", "--file", "--line-length"})
_SED_LONG = _SED_VALUE_LONG | {
    "--binary",
    "--debug",
    "--follow-symlinks",
    "--help",
    "--in-place",
    "--null-data",
    "--posix",
    "--quiet",
    "--regexp-extended",
    "--sandbox",
    "--separate",
    "--silent",
    "--unbuffered",
    "--version",
    "--zero-terminated",
}


def _sed_long_name(name: str) -> str:
    """Expand a getopt_long abbreviation (``--in`` -> ``--in-place``)."""
    if name in _SED_LONG:
        return name
    matches = [opt for opt in _SED_LONG if opt.startswith(name)]
    return matches[0] if len(matches) == 1 else name


def _sed_in_place_operands(args: list[str]) -> list[str]:
    """Files ``sed -i``/``--in-place`` edits (GNU option permutation honoured)."""
    in_place = False
    has_script = False
    operands: list[str] = []
    end_opts = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if end_opts or tok == "-" or not tok.startswith("-"):
            operands.append(tok)
        elif tok == "--":
            end_opts = True
        elif tok.startswith("--"):
            name, eq, _ = tok.partition("=")
            name = _sed_long_name(name)
            if name == "--in-place":
                in_place = True
            elif name in _SED_VALUE_LONG:
                has_script = has_script or name != "--line-length"
                if not eq:
                    i += 1
        else:
            for pos, ch in enumerate(tok[1:], 1):
                if ch == "i":
                    in_place = True
                    break  # the rest of the cluster is the backup suffix
                if ch in "ef":
                    has_script = True
                    if pos + 1 == len(tok):
                        i += 1  # value is the next argument
                    break
                if ch == "l":
                    if pos + 1 == len(tok) and i < len(args) and not args[i].startswith("-"):
                        i += 1  # optional numeric value
                    break
    if not in_place:
        return []
    operands = [t for t in operands if t]  # BSD ``sed -i '' ...``
    return operands if has_script else operands[1:]


@dataclass(frozen=True)
class _PerlishSpec:
    """Options of perl/ruby: ``code`` letters take a program, ``rest`` letters
    take the rest of the cluster (or the next argument when ``rest_next`` and
    nothing is attached), ``digits`` letters take trailing digits only.
    ``value_long`` options take the next argument unless written ``--opt=v``.
    A separate value is only skipped when it does not look like an option, so
    scanning errs towards still seeing a later ``-i``/``-e``."""

    code: str
    rest: str
    rest_next: str
    digits: str
    value_long: frozenset[str] = frozenset()


_PERLISH_SPECS = {
    "perl": _PerlishSpec(code="eE", rest="iIMmxdDF", rest_next="IMmx", digits="0lC"),
    "ruby": _PerlishSpec(
        code="e",
        rest="iIrCEFxWK",
        rest_next="IrCEFxW",
        digits="0",
        value_long=frozenset(
            {
                "--enable",
                "--disable",
                "--encoding",
                "--external-encoding",
                "--internal-encoding",
                "--dump",
                "--backtrace-limit",
                "--crash-report",
                "--parser",
            }
        ),
    ),
}


def _takes_next(args: list[str], i: int) -> bool:
    """True if ``args[i]`` exists and can be a separate option value."""
    return i < len(args) and not args[i].startswith("-")


def _perlish_options(cmd: str, args: list[str]) -> tuple[bool, list[str], list[str]]:
    """(in-place?, code strings, operands) for a perl/ruby command line."""
    spec = _PERLISH_SPECS[cmd]
    in_place = False
    codes: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            i += 1
            break
        if tok == "-" or not tok.startswith("-"):
            break
        i += 1
        if tok.startswith("--"):
            if tok in spec.value_long and _takes_next(args, i):
                i += 1
            continue
        pos = 1
        while pos < len(tok):
            ch = tok[pos]
            pos += 1
            if ch in spec.code:
                code = tok[pos:]
                if not code and i < len(args):
                    code, i = args[i], i + 1
                codes.append(code)
                break
            if ch in spec.rest:
                in_place = in_place or ch == "i"
                if ch in spec.rest_next and pos == len(tok) and _takes_next(args, i):
                    i += 1
                break
            if ch in spec.digits:
                while pos < len(tok) and tok[pos].isdigit():
                    pos += 1
    return in_place, codes, args[i:]


def _in_place_operands(cmd: str, args: list[str]) -> list[str]:
    """Files edited in place by ``sed -i``, ``perl -i`` or ``ruby -i``; [] otherwise."""
    if cmd in _SED_COMMANDS:
        return _sed_in_place_operands(args)
    if cmd not in _PERLISH_SPECS:
        return []
    in_place, codes, operands = _perlish_options(cmd, args)
    if not in_place:
        return []
    return operands if codes else operands[1:]  # without -e the first operand is the script


# Kept in sync with _PROTECTED_DIRS. The guards stop ``.gitignore``,
# ``.github`` and ``.git-blame-ignore-revs`` from matching.
_PROTECTED_CODE_RE = re.compile(
    r"(?<![\w.-])\.(?:"
    + "|".join(re.escape(d[1:]) for d in sorted(_PROTECTED_DIRS))
    + r")(?![\w.-])",
    re.IGNORECASE,
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SHELL_VALUE_LONG = frozenset({"--rcfile", "--init-file"})
_NODE_LIKE = frozenset({"node", "nodejs", "bun", "deno"})
_NODE_VALUE_OPTS = frozenset(
    {
        "-r",
        "--require",
        "--import",
        "--loader",
        "-C",
        "--conditions",
        "--experimental-loader",
        "--input-type",
    }
)


def _shell_code(args: list[str]) -> list[str]:
    has_c = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok in ("--", "-"):
            break
        if tok.startswith("--"):
            if tok in _SHELL_VALUE_LONG:
                i += 1
        elif tok[:1] in "-+" and len(tok) > 1:
            has_c = has_c or "c" in tok[1:]
            if ("o" in tok[1:] or "O" in tok[1:]) and i < len(args):
                i += 1
        else:
            return [tok] if has_c else []
    return [args[i]] if has_c and i < len(args) else []


def _python_code(args: list[str]) -> list[str]:
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok in ("--", "-") or not tok.startswith("-"):
            return []
        if tok.startswith("--"):
            if tok == "--check-hash-based-pycs":
                i += 1
            continue
        for pos, ch in enumerate(tok[1:], 1):
            if ch == "c":
                code = tok[pos + 1 :]
                if not code and i < len(args):
                    code = args[i]
                return [code]
            if ch == "m":
                return []
            if ch in "WX":
                if pos + 1 == len(tok) and i < len(args):
                    i += 1
                break
    return []


def _node_code(cmd: str, args: list[str]) -> list[str]:
    if cmd == "deno" and args[:1] == ["eval"]:
        return [" ".join(args[1:])]
    # Node-style CLIs have many value options (``--title x``, ...), so keep
    # scanning past non-option words rather than guess where the script is.
    codes: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if tok == "--":
            break
        name, eq, value = tok.partition("=")
        if name in ("--eval", "--print") and eq:
            codes.append(value)
        elif tok in ("-e", "--eval", "-p", "--print", "-pe", "-ep") and i < len(args):
            codes.append(args[i])
            i += 1
        elif tok in _NODE_VALUE_OPTS and i < len(args):
            i += 1
    return codes


def _interpreter_code(cmd: str, args: list[str]) -> list[str]:
    """Program text passed on the command line (``-c``, ``-e``, ``--eval``, eval, ...)."""
    if cmd == "eval":
        return [" ".join(args)]
    if cmd in _SHELLS:
        return _shell_code(args)
    if cmd.startswith("python"):
        return _python_code(args)
    if cmd in _NODE_LIKE:
        return _node_code(cmd, args)
    if cmd in _PERLISH_SPECS:
        return _perlish_options(cmd, args)[1]
    return []


def _check_unrestricted_bash(command: str, cwd: str | None) -> Decision | None:
    """Best-effort deny-list for implementer/test-runner Bash; None if acceptable.

    Denies git push, git reset --hard, git clean -f, curl/wget piped or
    substituted into a shell/interpreter, shell access to secret paths, and
    writes (redirections, tee, rm/mv/cp/..., ``sed -i``/``perl -i``/``ruby -i``)
    outside ``cwd`` or into ``.git``/``.carcara``/``.claude``. Interpreter
    code strings (``sh -c``, ``python -c``, ``node -e``, ``perl -e``,
    ``eval``, ...) naming one of those directories are denied outright, even
    read-only ones such as ``bash -c 'ls .git'``. Reads outside ``cwd`` stay
    allowed. Trivially bypassable (variables, scripts, cd): defence in depth only.
    """
    segments = _shell_segments(command)
    if segments is None:
        return _deny("could not parse shell command")
    fetching = False
    any_exec = False
    for op, tokens in segments:
        for tok in tokens:
            pieces = re.split(r"[:=]", tok)
            if any(is_secret_path(p, cwd) for p in pieces) or (
                _GLOB_CHARS & set(tok) and _mentions_secret(tok)
            ):
                return _deny(f"shell access to secret files is denied: {tok}")
        argv, targets = _split_redirects(tokens)
        argv = _strip_wrappers(argv)
        if not argv:
            continue
        cmd = posixpath.basename(argv[0])
        if cmd == "git":
            sub, rest = _git_subcommand(argv[1:])
            if sub == "push":
                return _deny("git push is denied")
            if sub == "reset" and "--hard" in rest:
                return _deny("git reset --hard is denied")
            if sub == "clean" and any(
                t == "--force" or (t.startswith("-") and not t.startswith("--") and "f" in t)
                for t in rest
            ):
                return _deny("git clean -f is denied")
        if op != "|":
            fetching = False
        if cmd in NETWORK_FETCHERS:
            fetching = True
        elif _is_exec_target(argv[0]):
            any_exec = True
            if fetching:
                return _deny(f"piping downloaded content into {cmd} is denied")
        if cmd == "tee":
            targets += [a for a in argv[1:] if not a.startswith("-") and a not in _DEV_SINKS]
        elif cmd in SHELL_WRITE_COMMANDS:
            targets += _write_operands(cmd, argv[1:])
        targets += _in_place_operands(cmd, argv[1:])
        for code in _interpreter_code(cmd, argv[1:]):
            if _PROTECTED_CODE_RE.search(code):
                return _deny(f"interpreter code naming .git/.carcara/.claude is denied: {cmd}")
        for target in targets:
            problem = _write_path_problem(target, cwd)
            if problem:
                return _deny(f"shell write {problem}: {target}")
    if any_exec and _FETCH_SUBST.search(command):
        return _deny("executing downloaded content (curl/wget substitution) is denied")
    return None


def decide(
    role: Role | str,
    tool_name: str,
    tool_input: Mapping[str, Any] | None,
    cwd: str | None,
    *,
    unrestricted_bash: bool = False,
) -> Decision:
    """Pure policy decision for one tool call by ``role``.

    ``unrestricted_bash`` (from ``carcara run --unrestricted-bash``) skips the
    implementer/test-runner Bash deny-list; read-only roles are unaffected.
    """
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
        if not unrestricted_bash:
            denied = _check_unrestricted_bash(command, cwd)
            if denied:
                return denied
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


def make_pre_tool_use_hook(
    role: Role | str, cwd: str | None, *, unrestricted_bash: bool = False
) -> HookCallback:
    """PreToolUse hook: deny per ``decide``; ``{}`` (pass through) otherwise."""

    async def hook(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        decision = decide(
            role,
            input_data.get("tool_name", ""),
            input_data.get("tool_input"),
            cwd,
            unrestricted_bash=unrestricted_bash,
        )
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
    role: Role | str, cwd: str | None, *, unrestricted_bash: bool = False
) -> Callable[[str, dict[str, Any], Any], Awaitable[Any]]:
    """Secondary ``can_use_tool`` callback mirroring ``decide`` (SDK imported lazily)."""

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        decision = decide(role, tool_name, tool_input, cwd, unrestricted_bash=unrestricted_bash)
        if decision.allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"carcara policy: {decision.reason}")

    return can_use_tool
