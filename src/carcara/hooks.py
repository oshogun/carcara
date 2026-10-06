"""Claude Code hook handlers for interactive sessions (``carcara hook ...``).

Wired into ``.claude/settings.json`` by the installer. Handlers return hook
JSON (``{}`` = pass through) and the CLI wrapper always exits 0 and fails
open, so a broken carcara never blocks the user's session. Imports stay light
(no SDK, no orchestrator): these run on every matched tool call.

- ``pre_tool_use``: the sole approver of carcara's own commands (project
  ``permissions.allow`` rules are ignored until the folder is trusted, and the
  installer adds none for carcara): allows the exact command forms the skill
  uses and the ``carcara`` skill itself, asks for every other command that
  invokes carcara, for approval/billing/budget flags next to any carcara
  mention and for resuming a budget-exceeded run (from any session); blocks
  main-session edits of project files (routing them through ``carcara run``)
  and any session's writes to the routing config; optionally applies the
  carcara policy to carcara-role subagents.
- ``prompt_context``: adds a short routing reminder plus paused runs.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from carcara import policy
from carcara.runstore import RunStore

STAGE_ENV_VAR = "CARCARA_STAGE"  # mirrors carcara.backend.STAGE_ENV_VAR (kept import-light)
OFF_ENV_VAR = "CARCARA_OFF"
ROUTING_OFF_FILE = "routing-off"
STRICT_POLICY_FILE = "strict-policy"
PAUSED_STATUSES = ("awaiting_approval", "needs_human", "budget_exceeded")
MAX_PAUSED = 10
MAX_CONTEXT = 1000

EDIT_DENY_REASON = (
    "carcara routing: code changes in this project go through the carcara orchestrator "
    "— use the carcara skill (carcara run). To edit directly: `carcara routing off` "
    "or CARCARA_OFF=1."
)
ROUTING_LINE = (
    "carcara routing is active: code-change requests go through the carcara skill "
    "(`carcara run`); questions don't."
)
# `carcara run` flags that need the human's confirmation, with the reason.
SENSITIVE_RUN_FLAGS = {
    "--yes": "approving a plan",
    "--accept-failures": "accepting failures",
    "--use-api-key": "API billing",
    "--max-budget-usd": "changing the run budget",
}
BUDGET_RESUME_REASON = "carcara: resuming a run that hit its budget needs your confirmation"
CONFIG_DENY_REASON = (
    "carcara routing: .claude/settings*.json and .claude/skills/carcara/ are protected "
    "while routing is on (`carcara routing off` or CARCARA_OFF=1 to change them)."
)
UNRECOGNISED_REASON = "carcara: unrecognised carcara command form; confirm it"
SKILL_NAME = "carcara"
_EXEMPT_ROOT_FILES = frozenset({"claude.md", "claude.local.md"})
_MENTION_RE = re.compile(r"carcara", re.IGNORECASE)
_LONG_OPT_RE = re.compile(r"--[A-Za-z][\w-]*")
_HEREDOC_RE = re.compile(r"\s*<<(['\"])([A-Za-z_][A-Za-z0-9_]*)\1\s*$")
_SHELL_META = frozenset(";&|$`<>()\\\n\r")
_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_NUM_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
# Whitelisted options per subcommand: option -> value kind (None = no value).
_RUN_OPTS: dict[str, str | None] = {
    "--allow-dirty": None,
    "--reject": None,
    "--plan-only": None,
    "--dry-run": None,
    "--list": None,
    "--review": None,
    "--profile": "value",
    "-p": "value",
    "--size": "size",
    "--resume": "value",
    "--feedback": "stdin",
}
_STATUS_OPTS: dict[str, str | None] = {"--json": None, "--plan": None}
_DIFF_OPTS: dict[str, str | None] = {"--stat": None}
_FALSY = frozenset({"", "0", "false", "no", "off"})
# Words after which the next word is still in command position.
_CMD_PREFIX_WORDS = frozenset(
    {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "exec", "eval"}
    | {"env", "sudo", "nohup", "time", "xargs", "command", "builtin", "nice", "stdbuf"}
)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_C_RE = re.compile(r"^-[a-z]*c$")  # bash -c / sh -lc / zsh -ec ...
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in _FALSY


def _carcara_dir(project: str | os.PathLike[str]) -> Path:
    return Path(project) / ".carcara"


def routing_off_reason(project: str | os.PathLike[str]) -> str | None:
    """Why routing is disabled (env or file), or None when it is on."""
    if _env_truthy(OFF_ENV_VAR):
        return f"{OFF_ENV_VAR} is set"
    if (_carcara_dir(project) / ROUTING_OFF_FILE).exists():
        return f".carcara/{ROUTING_OFF_FILE} exists"
    return None


def routing_enabled(project: str | os.PathLike[str]) -> bool:
    return routing_off_reason(project) is None


def strict_policy_enabled(project: str | os.PathLike[str]) -> bool:
    return (_carcara_dir(project) / STRICT_POLICY_FILE).exists()


def _pre_tool_output(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def _carcara_roles() -> frozenset[str]:
    from carcara.roles import load_roles

    return frozenset(load_roles())


def _same_path(a: str, b: str) -> bool:
    """Same file system object (case-insensitive FS aware), else normcase equality."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(a) == os.path.normcase(b)


def _parts_under(path: str, root: str) -> tuple[str, ...] | None:
    """Path components of ``path`` below ``root`` (``()`` for root itself), or None.

    Walks ``path`` and its parents, matching each against ``root`` by
    ``os.path.samefile`` so case-insensitive file systems (macOS, Windows)
    cannot be bypassed with a different spelling of the project directory.
    """
    rest: list[str] = []
    cur = path
    while True:
        if os.path.normcase(cur) == os.path.normcase(root) or (
            os.path.exists(cur) and _same_path(cur, root)
        ):
            return tuple(reversed(rest))
        parent, name = os.path.split(cur)
        if parent == cur or not name:
            return None
        rest.append(name)
        cur = parent


def _is_within(path: str, root: str) -> bool:
    return _parts_under(path, root) is not None


def _project_parts(path: Any, cwd: str, project: str) -> tuple[str, ...] | None:
    """Lower-cased components of ``path`` below the project; None/() outside or at root."""
    if not isinstance(path, str) or not path:
        return None
    root = os.path.realpath(project)
    target = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
    parts = _parts_under(target, root)
    return tuple(p.lower() for p in parts) if parts is not None else None


def _is_routing_config(low: tuple[str, ...]) -> bool:
    """``.claude/settings*.json`` or ``.claude/skills/carcara/**``."""
    if not low or low[0] != ".claude":
        return False
    if len(low) == 2 and fnmatch.fnmatchcase(low[1], "settings*.json"):
        return True
    return low[1:3] == ("skills", "carcara")


def _blocked_edit(path: Any, cwd: str, project: str) -> bool:
    """True when a main-session write to ``path`` must go through carcara."""
    low = _project_parts(path, cwd, project)
    if not low:  # outside the project, or the project root itself
        return False
    if low[0] == ".claude":
        # Routing config stays out of the model's reach; the rest of .claude/ is free.
        return _is_routing_config(low)
    return not (len(low) == 1 and low[0] in _EXEMPT_ROOT_FILES)


def _write_path(tool: Any, tool_input: dict[str, Any]) -> Any:
    path = tool_input.get("notebook_path" if tool == "NotebookEdit" else "file_path")
    if path is None:
        path = tool_input.get("file_path", tool_input.get("notebook_path"))
    return path


def _dequoted(command: str) -> str:
    return command.translate({ord(c): None for c in "'\"\\"})


def mentions_carcara(command: str) -> bool:
    """Loose check: ``carcara`` anywhere, also split by quotes/backslashes."""
    return bool(_MENTION_RE.search(command) or _MENTION_RE.search(_dequoted(command)))


class _Word:
    """A shell word: dequoted text, command position, and executed inner text."""

    __slots__ = ("cmd", "inner", "text")

    def __init__(self, cmd: bool) -> None:
        self.text = ""
        self.cmd = cmd
        self.inner: list[str] = []  # double-quoted segments with $( or ` (re-scanned)


def _scan_words(command: str) -> list[_Word] | None:
    """Split ``command`` into dequoted words; None when quoting is unbalanced.

    Words after ``;&|()`` / backtick / newline / reserved words / wrappers are
    in command position, as are leading ``VAR=value`` assignments.
    """
    words: list[_Word] = []
    cur: _Word | None = None
    cmd_next = True
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal cur, cmd_next
        if cur is None:
            return
        words.append(cur)
        # Reserved words, wrappers and their options, and VAR=value assignments
        # keep the command position for the next word.
        cmd_next = cur.cmd and (
            cur.text.lower() in _CMD_PREFIX_WORDS
            or cur.text.startswith("-")
            or bool(_ASSIGN_RE.match(cur.text))
        )
        cur = None

    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                cur = cur or _Word(cmd_next)
                cur.text += command[i + 1]
            i += 2
            continue
        if c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None
            cur = cur or _Word(cmd_next)
            cur.text += command[i + 1 : end]
            i = end + 1
            continue
        if c == '"':
            j = i + 1
            seg = ""
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n:
                    nxt = command[j + 1]
                    if nxt != "\n":
                        seg += nxt if nxt in '"\\$`' else "\\" + nxt
                    j += 2
                    continue
                seg += command[j]
                j += 1
            if j >= n:
                return None
            cur = cur or _Word(cmd_next)
            cur.text += seg
            if "$(" in seg or "`" in seg:
                cur.inner.append(seg)
            i = j + 1
            continue
        if c == "#" and cur is None:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        if c in ";&|()`\n\r":
            flush()
            cmd_next = True
            i += 1
            continue
        if c == "$" and command.startswith("$(", i):
            flush()
            cmd_next = True
            i += 2
            continue
        cur = cur or _Word(cmd_next)
        cur.text += c
        i += 1
    flush()
    return words


def _is_carcara_word(text: str) -> bool:
    return text.lower().rsplit("/", 1)[-1] == "carcara"


def _after_shell(words: list[_Word], opt: int) -> bool:
    """True when option ``words[opt]`` belongs to a shell command (``bash -lc``).

    Walks back to the nearest command-position word (inclusive); any word in
    that span named like a shell counts, so ``bash -o pipefail -c`` and
    ``sudo bash -c`` are caught while ``grep -c`` is not.
    """
    for j in range(opt, -1, -1):
        if words[j].text.lower().rsplit("/", 1)[-1] in _SHELLS:
            return True
        if words[j].cmd:
            return False
    return False


def invokes_carcara(command: str, _depth: int = 0) -> bool:
    """True when ``command`` runs carcara (as a command word or ``-m carcara``).

    Mentions as plain arguments (``ls src/carcara``, commit messages, grep
    patterns) are not invocations. Text the shell executes (``bash -c``,
    ``eval``, ``$(...)`` inside double quotes) is scanned recursively.
    Unbalanced quoting falls back to the loose :func:`mentions_carcara`.
    """
    if _depth > 5:
        return mentions_carcara(command)
    words = _scan_words(command)
    if words is None:
        return mentions_carcara(command)
    for idx, word in enumerate(words):
        low = word.text.lower()
        prev = words[idx - 1].text.lower() if idx else ""
        if word.cmd and _is_carcara_word(word.text):
            return True
        if low == "-mcarcara" or low.startswith("-mcarcara."):
            return True
        if prev == "-m" and (low == "carcara" or low.startswith("carcara.")):
            return True
        nested = list(word.inner)
        if idx and (
            (_SHELL_C_RE.match(prev) and _after_shell(words, idx - 1))
            or (words[idx - 1].cmd and prev == "eval")
        ):
            nested.append(word.text)
        if any(invokes_carcara(text, _depth + 1) for text in nested):
            return True
    return False


def sensitive_run_flags(command: str) -> list[str]:
    """Approval/billing flags in ``command``, also when hidden by quoting.

    Scans shlex tokens and the raw string with quotes/backslashes removed, so
    ``--"yes"``, ``-""-yes``, variables, ``bash -c`` and abbreviations (>= 4
    chars) are all caught; false positives only cost a confirmation prompt.
    """
    candidates = [m.group(0) for m in _LONG_OPT_RE.finditer(_dequoted(command))]
    try:
        candidates += [t for t in shlex.split(command) if t.startswith("--")]
    except ValueError:
        pass
    found: list[str] = []
    for cand in candidates:
        opt = cand.split("=", 1)[0]
        for flag in SENSITIVE_RUN_FLAGS:
            if len(opt) >= 4 and flag.startswith(opt) and flag not in found:
                found.append(flag)
    return found


def _split_heredoc(command: str) -> tuple[str, bool] | None:
    """(head, has_heredoc) for one simple command, or None when it is not that.

    A heredoc is accepted only with a quoted delimiter (``<<'D'``/``<<"D"``) on
    the first line, closed by the last line and nowhere else; its body is opaque.
    """
    text = command[:-1] if command.endswith("\n") else command
    lines = text.split("\n")
    head = lines[0]
    match = _HEREDOC_RE.search(head)
    if match is None:
        return (head, False) if len(lines) == 1 else None
    delim = match.group(2)
    if len(lines) < 2 or lines[-1] != delim or delim in lines[1:-1]:
        return None
    return head[: match.start()], True


def _valid_args(sub: str, args: list[str]) -> int | None:
    """Number of ``-`` (stdin) arguments when ``args`` are whitelisted, else None."""
    opts = {"run": _RUN_OPTS, "status": _STATUS_OPTS, "diff": _DIFF_OPTS}[sub]
    stdin = 0
    positional = 0
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in opts:
            kind = opts[tok]
            if kind is not None:
                i += 1
                if i >= len(args):
                    return None
                value = args[i]
                ok = {
                    "value": bool(_VALUE_RE.match(value)) and not value.startswith("-"),
                    "size": value in ("S", "M", "L"),
                    "num": bool(_NUM_RE.match(value)),
                    "stdin": value == "-",
                }[kind]
                if not ok:
                    return None
                stdin += kind == "stdin"
        elif tok == "-" and sub == "run":
            stdin += 1
        elif sub != "run" and not tok.startswith("-") and _VALUE_RE.match(tok):
            positional += 1
            if positional > 1:
                return None
        else:
            return None
        i += 1
    return stdin


def _safe_form_tokens(command: str) -> list[str] | None:
    """Tokens of exactly one whitelisted ``carcara run|status|diff`` invocation, else None."""
    split = _split_heredoc(command)
    if split is None:
        return None
    head, heredoc = split
    if any(c in _SHELL_META for c in head):
        return None
    try:
        tokens = shlex.split(head)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0] != "carcara" or tokens[1] not in ("run", "status", "diff"):
        return None
    stdin = _valid_args(tokens[1], tokens[2:])
    return tokens if stdin is not None and stdin == (1 if heredoc else 0) else None


def _is_safe_form(command: str) -> bool:
    """True for exactly one whitelisted ``carcara run|status|diff`` invocation."""
    return _safe_form_tokens(command) is not None


def _budget_resume(tokens: list[str], project: str | None) -> bool:
    """True when ``carcara run --resume ID`` may continue a budget-capped run.

    Reads ``.carcara/runs/<ID>/state.json`` read-only; an unknown project, run
    or unreadable state counts as budget-exceeded (ask rather than allow).
    """
    if tokens[1] != "run" or "--resume" not in tokens:
        return False
    run_id = tokens[tokens.index("--resume") + 1]
    if project is None or run_id in (".", ".."):
        return True
    state = _read_state(RunStore(project).root / run_id / "state.json")
    return state is None or state.get("status") == "budget_exceeded"


def classify_carcara_command(
    command: str, project: str | os.PathLike[str] | None = None
) -> tuple[str, str] | None:
    """("allow"|"ask", reason) for a Bash command invoking carcara, else None.

    "allow" only for one exact whitelisted invocation (the forms the carcara
    skill uses) that does not resume a budget-exceeded run of ``project``;
    approval/billing/budget flags alongside any mention of carcara and every
    other invocation → "ask"; carcara only as an argument (paths, messages,
    patterns) → None. Never "deny": the human decides.
    """
    if not mentions_carcara(command):
        return None
    tokens = _safe_form_tokens(command)
    if tokens is not None:
        if _budget_resume(tokens, None if project is None else str(project)):
            return "ask", BUDGET_RESUME_REASON
        return "allow", "carcara: approved carcara command"
    flags = sensitive_run_flags(command)
    if flags:
        why = " / ".join(SENSITIVE_RUN_FLAGS[f] for f in flags)
        return "ask", f"carcara: {why} needs your confirmation ({', '.join(flags)})"
    if not invokes_carcara(command):
        return None
    return "ask", UNRECOGNISED_REASON


def pre_tool_use(data: dict[str, Any], project: str) -> dict[str, Any]:
    """PreToolUse handler; ``{}`` passes the call through."""
    if os.environ.get(STAGE_ENV_VAR):
        return {}  # inside `carcara run`: the in-process SDK hook enforces policy
    tool = data.get("tool_name")
    tool_input = data.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else project
    agent_type = data.get("agent_type")
    subagent = bool(agent_type or data.get("agent_id"))
    routing = routing_enabled(project)
    if tool == "Bash" and isinstance(tool_input.get("command"), str):
        verdict = classify_carcara_command(tool_input["command"], project)
        if verdict is not None:
            decision, reason = verdict
            # The hook is the sole approver of carcara commands, but only for the
            # main session with routing on; elsewhere safe forms take the normal path.
            if decision == "ask" or (routing and not subagent):
                return _pre_tool_output(decision, reason)
    if tool == "Skill" and not subagent and routing and tool_input.get("skill") == SKILL_NAME:
        return _pre_tool_output("allow", "carcara: routing skill")
    if subagent:
        # Routing config is off-limits to every subagent; other writes take the normal path.
        if (
            routing
            and tool in policy.WRITE_TOOLS
            and _is_routing_config(
                _project_parts(_write_path(tool, tool_input), cwd, project) or ()
            )
        ):
            return _pre_tool_output("deny", CONFIG_DENY_REASON)
        if (
            isinstance(agent_type, str)
            and strict_policy_enabled(project)
            and agent_type in _carcara_roles()
        ):
            decision = policy.decide(agent_type, str(tool or ""), tool_input, cwd)
            if not decision.allow:
                return _pre_tool_output("deny", f"carcara policy: {decision.reason}")
        return {}
    if not routing:
        return {}
    if tool in policy.WRITE_TOOLS:
        if _blocked_edit(_write_path(tool, tool_input), cwd, project):
            return _pre_tool_output("deny", EDIT_DENY_REASON)
    return {}


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _excerpt(text: Any, limit: int = 80) -> str:
    return " ".join(str(text or "").split())[:limit]


def prompt_context(data: dict[str, Any], project: str) -> dict[str, Any]:
    """UserPromptSubmit handler: routing reminder plus active/paused runs."""
    if os.environ.get(STAGE_ENV_VAR) or not routing_enabled(project):
        return {}
    lines = [ROUTING_LINE]
    store = RunStore(project)  # read-only use: never creates .carcara
    if store.root.is_dir():
        active = store.active()
        if active:
            lines.append(f"active run: {active.get('run_id')}")
        paused: list[str] = []
        for run_id in reversed(store.list_runs()):
            state = _read_state(store.root / run_id / "state.json")
            if state and state.get("status") in PAUSED_STATUSES:
                paused.append(f"{run_id} {state['status']}: {_excerpt(state.get('task'))}")
                if len(paused) >= MAX_PAUSED:
                    break
        hint = "details: `carcara status <id>`"
        if paused:
            # Keep the hint: drop the oldest paused runs that would not fit.
            budget = MAX_CONTEXT - len("\n".join([*lines, "paused runs:", hint])) - 1
            kept = []
            for line in paused:
                if len(line) + 1 > budget:
                    break
                kept.append(line)
                budget -= len(line) + 1
            if kept:
                lines.append("paused runs:")
                lines.extend(kept)
        if active or paused:
            lines.append(hint)
    text = "\n".join(lines)
    if len(text) >= MAX_CONTEXT:
        text = text[: MAX_CONTEXT - 4] + "..."
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


HANDLERS = {"pre-tool-use": pre_tool_use, "prompt-context": prompt_context}


def hook_main(argv: list[str]) -> int:
    """``carcara hook NAME [--project DIR]``: JSON on stdin; always exit 0 (fail open)."""
    try:
        import argparse

        parser = argparse.ArgumentParser(prog="carcara hook")
        parser.add_argument("name", choices=sorted(HANDLERS))
        parser.add_argument("--project", default=None, metavar="DIR")
        ns = parser.parse_args(argv)
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict):
            return 0
        project = (
            ns.project
            or os.environ.get("CLAUDE_PROJECT_DIR")
            or (data.get("cwd") if isinstance(data.get("cwd"), str) else None)
            or os.getcwd()
        )
        result = HANDLERS[ns.name](data, project)
        if result:
            sys.stdout.write(json.dumps(result) + "\n")
    except (Exception, SystemExit):
        pass
    return 0


def routing_main(action: str, project: str) -> int:
    """``carcara routing on|off|status``."""
    base = _carcara_dir(project)
    flag = base / ROUTING_OFF_FILE
    if action == "off":
        base.mkdir(parents=True, exist_ok=True)
        ignore = base / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        flag.write_text("", encoding="utf-8")
    elif action == "on":
        flag.unlink(missing_ok=True)
    reason = routing_off_reason(project)
    if reason is None:
        print("routing: on")
    else:
        print(f"routing: off ({reason})")
    return 0
