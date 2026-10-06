"""Install carcara's agents, commands, skill, settings and CLAUDE.md section.

Messages, ordering, dry-run, skip/--force and the CLAUDE.md markers follow the
0.1.0 bash installer; since 0.3.0 the output differs from it:

- ``.claude/settings.json`` is merged into an existing file (user permissions,
  hooks and model kept; carcara-owned hook entries, i.e. those whose command
  contains ``carcara hook``, are replaced) instead of skipped/overwritten.
- Routing (default): the ``carcara`` skill and the hook groups route code
  changes to ``carcara run``; ``--no-routing`` leaves them out (and removes
  carcara-installed ones). No permission rules are added for carcara: its
  hook approves the exact command forms the skill uses (project allow rules
  only apply once the folder is trusted, and ``Bash(carcara run *)`` could be
  abused to self-approve). ``Bash(carcara ...)`` rules written by earlier 0.3
  dev installs are removed.
- ``--strict-policy`` adds Read/Grep/Glob hook groups and the
  ``.carcara/strict-policy`` flag file that applies the carcara policy to
  carcara-role subagents in interactive sessions; without it both are removed.
- ``.carcara/profile`` records the chosen profile (name, or absolute path of a
  custom file) so ``carcara run`` defaults to it.

All validation (CLAUDE.md markers, settings.json JSON/shape) happens before
any write.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from carcara import __version__
from carcara.profiles import (
    INSTALLED_PROFILE_REL,
    Profile,
    ProfileError,
    list_profiles,
    load_profile,
)
from carcara.resources import claude_md_template, iter_claude_templates, render, templates_root

BEGIN_MARKER = b"<!-- carcara:begin -->"
END_MARKER = b"<!-- carcara:end -->"

SETTINGS_REL = "settings.json"
SKILL_REL = "skills/carcara/SKILL.md"
SKILL_MARKER = "<!-- carcara:skill"
HOOK_MARK = "carcara hook"
HOOK_TIMEOUT = 10
# Allow rules written by earlier 0.3 dev installs; always removed on (re)install.
LEGACY_ALLOW = ("Bash(carcara run *)", "Bash(carcara status *)", "Bash(carcara diff *)")
STRICT_MATCHERS = ("Read", "Grep", "Glob")
STRICT_POLICY_REL = ".carcara/strict-policy"
CARCARA_GITIGNORE_REL = ".carcara/.gitignore"
ROUTING_PLACEHOLDER = "{{ROUTING}}"
ROUTING_ON_TEXT = """- Code changes in this repo are routed to the carcara orchestrator: use the
  `carcara` skill (it runs `carcara run`); don't edit project files directly.
  `/sdlc*` remain available as a manual path."""
ROUTING_OFF_TEXT = """- Triage first: do S-sized changes directly; only escalate to subagents when
  the task warrants it. Use the architect only for L-sized work."""

USAGE = f"""carcara {__version__} - agentic SDLC framework for Claude Code

Usage: carcara [options] [target-dir]

Installs subagents, slash commands, settings and a CLAUDE.md section into
<target-dir> (default: current directory).

Options:
  -p, --profile NAME   model profile: economy | balanced | quality, or a path
                       to a custom profile file (default: balanced)
  -f, --force          overwrite existing carcara files in .claude/
  -n, --dry-run        show what would be done without writing anything
      --no-routing     don't route code changes through `carcara run` (no
                       skill or hooks; removes them)
      --strict-policy  also apply the carcara policy to carcara subagents in
                       interactive sessions (Read/Grep/Glob hooks)
  -l, --list-profiles  list available profiles and their model routing
  -V, --version        print version
  -h, --help           show this help
"""


class InstallError(Exception):
    """Fatal installer error; printed as ``carcara: <msg>`` with exit 1."""


@dataclass
class InstallResult:
    written: int = 0
    skipped: int = 0


def _lines(data: bytes) -> list[bytes]:
    """Records as seen by grep/awk: split on ``\\n``, no trailing empty record."""
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def count_markers(path: str, data: bytes) -> int:
    """Return the number of carcara blocks (0 or 1); raise if markers are unbalanced."""
    lines = _lines(data)
    begins = [i for i, line in enumerate(lines) if line == BEGIN_MARKER]
    ends = [i for i, line in enumerate(lines) if line == END_MARKER]
    if len(begins) != len(ends) or len(begins) > 1 or (begins and begins[0] > ends[0]):
        raise InstallError(f"{path} has unbalanced carcara markers; fix them manually")
    return len(begins)


def _render_resource(res, profile: Profile) -> bytes:
    return render(res.read_bytes().decode("utf-8"), profile).encode("utf-8")


def _claude_md_block(profile: Profile, routing: bool = True) -> bytes:
    text = claude_md_template().read_bytes().decode("utf-8")
    text = text.replace(ROUTING_PLACEHOLDER, ROUTING_ON_TEXT if routing else ROUTING_OFF_TEXT)
    body = render(text, profile).encode("utf-8")
    return BEGIN_MARKER + b"\n" + body + END_MARKER + b"\n"


def _replace_block(data: bytes, block: bytes) -> bytes:
    """Port of the awk update: swap the marked block, normalising line endings."""
    out: list[bytes] = []
    skip = False
    inserted = False
    for line in _lines(data):
        if line == BEGIN_MARKER:
            if not inserted:
                out.append(block)
                inserted = True
            skip = True
        elif line == END_MARKER:
            skip = False
        elif not skip:
            out.append(line + b"\n")
    return b"".join(out)


def _action(name: str, dest: str) -> str:
    return f"  {name:<10} {dest}\n"


def _install_claude_md(
    dest: str, profile: Profile, routing: bool, dry_run: bool, out: TextIO
) -> None:
    block = _claude_md_block(profile, routing)
    if not os.path.exists(dest):
        out.write(_action("create", dest))
        if not dry_run:
            with open(dest, "wb") as fh:
                fh.write(block)
        return
    with open(dest, "rb") as fh:
        data = fh.read()
    if count_markers(dest, data) == 0:
        out.write(_action("append", dest))
        new = data + b"\n" + block
    else:
        out.write(_action("update", dest))
        new = _replace_block(data, block)
    if dry_run:
        return
    with open(dest, "wb") as fh:
        fh.write(new)


# --- settings.json ------------------------------------------------------------


def hook_command(name: str) -> str:
    """Shell command for a carcara hook; a no-op (exit 0) when carcara is not on PATH."""
    return (
        "command -v carcara >/dev/null 2>&1 || exit 0; "
        f'carcara hook {name} --project "${{CLAUDE_PROJECT_DIR}}"'
    )


def hook_group(name: str, matcher: str | None = None) -> dict[str, Any]:
    group: dict[str, Any] = {} if matcher is None else {"matcher": matcher}
    group["hooks"] = [{"type": "command", "command": hook_command(name), "timeout": HOOK_TIMEOUT}]
    return group


def is_carcara_hook(hook: Any) -> bool:
    """A hook entry is carcara-owned when its command runs ``carcara hook``."""
    return isinstance(hook, dict) and HOOK_MARK in str(hook.get("command", ""))


def is_carcara_group(group: Any) -> bool:
    """A hook group is carcara-owned when any of its commands runs ``carcara hook``."""
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(is_carcara_hook(h) for h in hooks)


def _strip_carcara_hooks(group: dict[str, Any]) -> dict[str, Any] | None:
    """``group`` without carcara hook entries; None when nothing else is left."""
    hooks = group.get("hooks")
    if not isinstance(hooks, list) or not any(is_carcara_hook(h) for h in hooks):
        return group
    kept = [h for h in hooks if not is_carcara_hook(h)]
    return {**group, "hooks": kept} if kept else None


def _dump_settings(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _carcara_settings(profile: Profile, routing: bool, strict: bool) -> dict[str, Any]:
    """The rendered settings template, adjusted for routing / strict policy."""
    res = templates_root().joinpath("claude", SETTINGS_REL)
    data = json.loads(_render_resource(res, profile).decode("utf-8"))
    if not routing:
        data.pop("hooks", None)
    elif strict:
        pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
        pre.extend(hook_group("pre-tool-use", m) for m in STRICT_MATCHERS)
    return data


def _load_settings(path: str) -> tuple[bytes, dict[str, Any]]:
    """Read and validate an existing settings.json; InstallError on bad JSON/shape."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); fix it or move it away") from exc

    def bad(what: str) -> InstallError:
        return InstallError(f"{path}: {what}; fix it or move it away")

    if not isinstance(data, dict):
        raise bad("expected a JSON object")
    perms = data.get("permissions")
    if perms is not None:
        if not isinstance(perms, dict):
            raise bad('"permissions" must be an object')
        for key in ("allow", "deny"):
            if key in perms and not isinstance(perms[key], list):
                raise bad(f'"permissions.{key}" must be a list')
    hooks = data.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            raise bad('"hooks" must be an object')
        for event, groups in hooks.items():
            if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
                raise bad(f'"hooks.{event}" must be a list of objects')
    return raw, data


def merge_settings(
    existing: dict[str, Any], carcara: dict[str, Any], *, force: bool
) -> dict[str, Any]:
    """Merge carcara's settings into ``existing`` (which is not modified).

    Permissions are unioned (user order kept, new entries appended; legacy
    carcara allow rules dropped), carcara hook entries are replaced (a group is
    dropped only when nothing else is left in it), ``model`` is only set when
    absent or with force.
    """
    out = copy.deepcopy(existing)
    if "model" in carcara and ("model" not in out or force):
        out["model"] = carcara["model"]
    wanted = carcara.get("permissions", {})
    if wanted or isinstance(out.get("permissions"), dict):
        perms = out.setdefault("permissions", {})
        if isinstance(perms.get("allow"), list):
            perms["allow"] = [a for a in perms["allow"] if a not in LEGACY_ALLOW]
        for key, values in wanted.items():
            current = perms.setdefault(key, [])
            current.extend(v for v in values if v not in current)
    hooks = out.get("hooks")
    emptied: set[str] = set()
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            kept = [g for g in map(_strip_carcara_hooks, groups) if g is not None]
            if groups and not kept:
                emptied.add(event)
            hooks[event] = kept
    for event, groups in carcara.get("hooks", {}).items():
        out.setdefault("hooks", {}).setdefault(event, []).extend(copy.deepcopy(groups))
    hooks = out.get("hooks")
    if emptied and isinstance(hooks, dict):
        for event in emptied:
            if not hooks[event]:
                del hooks[event]
        if not hooks:
            del out["hooks"]
    return out


def _plan_settings(dest: str, carcara: dict[str, Any], force: bool) -> tuple[str, bytes | None]:
    """(action, bytes to write or None) for ``dest``; validates before any write."""
    if not os.path.exists(dest):
        return "create", _dump_settings(carcara)
    raw, existing = _load_settings(dest)
    new = _dump_settings(merge_settings(existing, carcara, force=force))
    if new == raw:
        return "up-to-date", None
    return "merge", new


def _is_carcara_skill(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return False
    parts = text.split("---\n", 2)
    if len(parts) < 3 or parts[0] != "":
        return False
    return "name: carcara" in parts[1].splitlines() and SKILL_MARKER in parts[2]


def _write(dest: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)


def _write_atomic(dest: str, data: bytes) -> None:
    """Write via a temp file in the same directory + ``os.replace``."""
    dest = os.path.realpath(dest)  # keep a symlinked settings.json a symlink
    directory = os.path.dirname(dest) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        mode = os.stat(dest).st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove_skill(dest: str) -> None:
    os.unlink(dest)
    for d in (os.path.dirname(dest), os.path.dirname(os.path.dirname(dest))):
        try:
            os.rmdir(d)
        except OSError:
            break


def _install_carcara_gitignore(target: str, dry_run: bool, out: TextIO) -> None:
    ignore = f"{target}/{CARCARA_GITIGNORE_REL}"
    if not os.path.exists(ignore):
        out.write(_action("create", ignore))
        if not dry_run:
            _write(ignore, b"*\n")


def _install_strict_flag(target: str, strict: bool, dry_run: bool, out: TextIO) -> None:
    flag = f"{target}/{STRICT_POLICY_REL}"
    if strict:
        if not os.path.exists(flag):
            out.write(_action("create", flag))
            if not dry_run:
                _write(flag, b"")
    elif os.path.exists(flag):
        out.write(_action("remove", flag))
        if not dry_run:
            os.unlink(flag)


def _install_profile_record(
    target: str, profile_spec: str, profile: Profile, dry_run: bool, out: TextIO
) -> None:
    """Record the profile for ``carcara run``. Gitignored on purpose: a custom
    profile is recorded as an absolute, machine-local path."""
    # Same test as load_profile: a file spec (even one named like a built-in)
    # is recorded by path, a packaged profile by name.
    is_file = os.path.isfile(profile_spec)
    spec = os.path.abspath(profile.source) if is_file else profile.name
    data = f"{spec}\n".encode()
    path = f"{target}/{INSTALLED_PROFILE_REL}"
    if os.path.exists(path):
        with open(path, "rb") as fh:
            if fh.read() == data:
                return
        action = "overwrite"
    else:
        action = "create"
    out.write(_action(action, path))
    if not dry_run:
        _write(path, data)


def _is_user_config_dir(target: str) -> bool:
    """True if ``target`` is the home directory, the Claude config dir or inside ~/.claude."""
    real = os.path.realpath(target)
    home = os.path.realpath(Path.home())
    claude_dir = os.path.join(home, ".claude")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    forbidden = {home, claude_dir}
    if config_dir:
        forbidden.add(os.path.realpath(os.path.expanduser(config_dir)))
    return real in forbidden or real.startswith(claude_dir + os.sep)


def install(
    target: str = ".",
    profile_spec: str = "balanced",
    *,
    force: bool = False,
    dry_run: bool = False,
    routing: bool = True,
    strict_policy: bool = False,
    out: TextIO | None = None,
) -> InstallResult:
    """Install into ``target``. Raises InstallError/ProfileError before any write."""
    out = sys.stdout if out is None else out
    target = target or "."
    if not templates_root().joinpath("claude").is_dir():
        raise InstallError(f"templates not found in {templates_root()}")
    if not os.path.isdir(target):
        raise InstallError(f"target directory does not exist: {target}")
    if _is_user_config_dir(target):
        raise InstallError(
            "refusing to install into your home directory (Claude Code would load it as "
            "user-level config for every project); pass a project directory"
        )
    if strict_policy and not routing:
        raise InstallError("--strict-policy cannot be combined with --no-routing")
    profile = load_profile(profile_spec)
    claude_md = f"{target}/CLAUDE.md"
    if os.path.exists(claude_md):
        with open(claude_md, "rb") as fh:
            count_markers(claude_md, fh.read())
    settings_dest = f"{target}/.claude/{SETTINGS_REL}"
    settings = _plan_settings(
        settings_dest, _carcara_settings(profile, routing, strict_policy), force
    )

    suffix = " (dry run)" if dry_run else ""
    out.write(f"carcara {__version__}: installing profile '{profile.name}' into {target}{suffix}\n")

    result = InstallResult()
    for rel, res in iter_claude_templates():
        dest = f"{target}/.claude/{rel}"
        if rel == SETTINGS_REL:
            action, data = settings
            out.write(_action(action, dest))
            if data is not None:
                result.written += 1
                if not dry_run:
                    _write_atomic(dest, data)
            continue
        if rel == SKILL_REL and not routing:
            if os.path.isfile(dest) and _is_carcara_skill(dest):
                out.write(_action("remove", dest))
                if not dry_run:
                    _remove_skill(dest)
            continue
        if os.path.exists(dest):
            if not force:
                out.write(f"  skip       {dest} (exists; use --force to overwrite)\n")
                result.skipped += 1
                continue
            action = "overwrite"
        else:
            action = "create"
        out.write(_action(action, dest))
        result.written += 1
        if dry_run:
            continue
        _write(dest, _render_resource(res, profile))

    _install_claude_md(claude_md, profile, routing, dry_run, out)
    _install_carcara_gitignore(target, dry_run, out)
    _install_strict_flag(target, strict_policy, dry_run, out)
    _install_profile_record(target, profile_spec, profile, dry_run, out)

    out.write(f"done: {result.written} file(s) written, {result.skipped} skipped.\n")
    if result.skipped > 0:
        out.write(
            "note: skipped files keep their previous content; re-run with --force "
            f"to apply profile '{profile.name}' to them.\n"
        )
    if routing:
        out.write(
            f"Next: start Claude Code in {target} and just ask for a change "
            "\u2014 carcara routes it. (`carcara routing off` to disable.)\n"
        )
    else:
        out.write(f"Next: start Claude Code in {target} and run: /sdlc <task>\n")
    return result


def main(argv: list[str]) -> int:
    """``carcara install`` argument handling, mirroring the bash ``case`` loop."""
    profile = "balanced"
    target = ""
    force = False
    dry_run = False
    routing = True
    strict_policy = False
    args = list(argv)
    try:
        while args:
            arg = args[0]
            if arg in ("-p", "--profile"):
                if len(args) < 2:
                    raise InstallError(f"{arg} requires an argument")
                profile = args[1]
                del args[:2]
            elif arg.startswith("--profile="):
                profile = arg[len("--profile=") :]
                del args[0]
            elif arg in ("-f", "--force"):
                force = True
                del args[0]
            elif arg in ("-n", "--dry-run"):
                dry_run = True
                del args[0]
            elif arg == "--no-routing":
                routing = False
                del args[0]
            elif arg == "--strict-policy":
                strict_policy = True
                del args[0]
            elif arg in ("-l", "--list-profiles"):
                sys.stdout.write(list_profiles())
                return 0
            elif arg in ("-V", "--version"):
                print(f"carcara {__version__}")
                return 0
            elif arg in ("-h", "--help"):
                sys.stdout.write(USAGE)
                return 0
            elif arg == "--":
                del args[0]
                break
            elif arg.startswith("-"):
                raise InstallError(f"unknown option: {arg} (see --help)")
            else:
                if target:
                    raise InstallError("only one target directory may be given")
                target = arg
                del args[0]
        if args:
            if target or len(args) != 1:
                raise InstallError("only one target directory may be given")
            target = args[0]
        install(
            target or ".",
            profile,
            force=force,
            dry_run=dry_run,
            routing=routing,
            strict_policy=strict_policy,
        )
    except (InstallError, ProfileError) as exc:
        sys.stdout.flush()
        print(f"carcara: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        sys.stdout.flush()
        detail = exc.strerror or str(exc)
        where = f": {exc.filename}" if exc.filename else ""
        print(f"carcara: {detail}{where}", file=sys.stderr)
        return 1
    return 0
