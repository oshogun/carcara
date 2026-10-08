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
- ``.carcara/install.json`` (the manifest) records what install added to
  settings.json and CLAUDE.md, so ``carcara uninstall`` can undo exactly that.

All validation (CLAUDE.md markers, settings.json JSON/shape) happens before
any write.
"""

from __future__ import annotations

import copy
import glob
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from carcara import __version__, runstore
from carcara.profiles import (
    INSTALLED_PROFILE_REL,
    Profile,
    ProfileError,
    list_profiles,
    load_profile,
    read_installed_profile,
)
from carcara.resources import (
    claude_md_template,
    iter_claude_templates,
    profiles_root,
    render,
    templates_root,
)

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
# What install added (settings entries, CLAUDE.md mode), so uninstall can undo it.
INSTALL_MANIFEST_REL = ".carcara/install.json"
ROUTING_OFF_REL = ".carcara/routing-off"  # written by `carcara routing off`
RUNS_REL = ".carcara/runs"
ACTIVE_RUN_REL = ".carcara/active.json"
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
) -> str:
    """Install the CLAUDE.md block; returns the action (create/append/update)."""
    block = _claude_md_block(profile, routing)
    if not os.path.exists(dest):
        out.write(_action("create", dest))
        if not dry_run:
            with open(dest, "wb") as fh:
                fh.write(block)
        return "create"
    with open(dest, "rb") as fh:
        data = fh.read()
    if count_markers(dest, data) == 0:
        action = "append"
        new = data + b"\n" + block
    else:
        action = "update"
        new = _replace_block(data, block)
    out.write(_action(action, dest))
    if not dry_run:
        with open(dest, "wb") as fh:
            fh.write(new)
    return action


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


def _settings_additions(
    existing: dict[str, Any], carcara: dict[str, Any], *, force: bool
) -> dict[str, Any]:
    """What ``merge_settings`` adds to ``existing``, for the install manifest:
    ``permissions`` entries not already present and whether ``model`` is set."""
    perms_existing = existing.get("permissions")
    perms_existing = perms_existing if isinstance(perms_existing, dict) else {}
    added_perms = {
        key: [v for v in values if v not in (perms_existing.get(key) or [])]
        for key, values in carcara.get("permissions", {}).items()
    }
    hooks = existing.get("hooks")
    # Events holding only carcara hook groups are carcara's (merge drops them).
    hooks_existing = {
        event
        for event, groups in (hooks if isinstance(hooks, dict) else {}).items()
        if not groups or any(_strip_carcara_hooks(g) is not None for g in groups)
    }
    added_hook_events = [
        event for event in carcara.get("hooks", {}).keys() if event not in hooks_existing
    ]
    sets_model = "model" in carcara and ("model" not in existing or force)
    perms_created = "permissions" not in existing
    hooks_created = "hooks" not in existing or (
        bool(hooks) and isinstance(hooks, dict) and not hooks_existing
    )
    permission_keys = [key for key, values in added_perms.items() if key not in perms_existing]
    return {
        "permissions": added_perms,
        "model": carcara["model"] if sets_model else None,
        "model_was_absent": "model" not in existing,
        "permissions_created": perms_created,
        "hooks_created": hooks_created,
        "permission_keys": permission_keys,
        "hook_events": added_hook_events,
    }


def _plan_settings(
    dest: str, carcara: dict[str, Any], force: bool
) -> tuple[str, bytes | None, dict[str, Any]]:
    """(action, bytes to write or None, additions) for ``dest``; validates before any write."""
    if not os.path.exists(dest):
        return "create", _dump_settings(carcara), _settings_additions({}, carcara, force=force)
    raw, existing = _load_settings(dest)
    additions = _settings_additions(existing, carcara, force=force)
    new = _dump_settings(merge_settings(existing, carcara, force=force))
    if new == raw:
        return "up-to-date", None, additions
    return "merge", new, additions


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


def _recorded_spec(profile_spec: str, profile: Profile) -> str:
    # Same test as load_profile: a file spec (even one named like a built-in)
    # is recorded by path, a packaged profile by name.
    return os.path.abspath(profile.source) if os.path.isfile(profile_spec) else profile.name


def _load_manifest(target: str) -> dict[str, Any] | None:
    """The install manifest of ``target``; None if absent. InstallError if invalid."""
    path = f"{target}/{INSTALL_MANIFEST_REL}"
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); fix it or remove it") from exc
    perms = data.get("permissions_added") if isinstance(data, dict) else None
    if not isinstance(perms, dict) or not all(
        isinstance(v, list) and all(isinstance(e, str) for e in v) for v in perms.values()
    ):
        raise InstallError(f"{path}: unexpected content; fix it or remove it")
    if "settings_created_keys" in data and not _valid_created_keys(data["settings_created_keys"]):
        raise InstallError(f"{path}: unexpected content; fix it or remove it")
    return data


def _valid_created_keys(keys: Any) -> bool:
    def is_str_list(v: Any) -> bool:
        return isinstance(v, list) and all(isinstance(e, str) for e in v)

    return (
        isinstance(keys, dict)
        and isinstance(keys.get("permissions"), bool)
        and isinstance(keys.get("hooks"), bool)
        and is_str_list(keys.get("permission_keys"))
        and is_str_list(keys.get("hook_events"))
    )


def _created_keys(prev: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    """Which settings containers install created, so uninstall drops only those:
    whether it created ``permissions`` / ``hooks`` and the lists / events it added."""
    return {
        "permissions": prev.get("permissions", additions["permissions_created"]),
        "permission_keys": sorted(
            set(prev.get("permission_keys", [])) | set(additions["permission_keys"])
        ),
        "hooks": prev.get("hooks", additions["hooks_created"]),
        "hook_events": sorted(set(prev.get("hook_events", [])) | set(additions["hook_events"])),
    }


def _build_manifest(
    prev: dict[str, Any] | None,
    settings_action: str,
    additions: dict[str, Any],
    claude_md_mode: str,
    spec: str,
) -> dict[str, Any]:
    """Record what this install added, merged with an earlier manifest."""
    model = additions["model"]
    if prev is None:
        return {
            "profile": spec,
            "settings_created": settings_action == "create",
            "claude_md_mode": claude_md_mode,
            "model_set": model if additions["model_was_absent"] else None,
            "permissions_added": additions["permissions"],
            "settings_created_keys": _created_keys({}, additions),
        }
    perms = {k: list(v) for k, v in prev["permissions_added"].items()}
    for key, values in additions["permissions"].items():
        current = perms.setdefault(key, [])
        current.extend(v for v in values if v not in current)
    model_set = prev.get("model_set")
    if model is not None and (additions["model_was_absent"] or model_set is not None):
        model_set = model
    manifest = {
        "profile": spec,
        "settings_created": bool(prev.get("settings_created")),
        "claude_md_mode": prev.get("claude_md_mode", claude_md_mode),
        "model_set": model_set,
        "permissions_added": perms,
    }
    # A manifest from before settings_created_keys keeps the old behaviour.
    if "settings_created_keys" in prev:
        manifest["settings_created_keys"] = _created_keys(prev["settings_created_keys"], additions)
    return manifest


def _install_manifest(target: str, manifest: dict[str, Any], dry_run: bool, out: TextIO) -> None:
    path = f"{target}/{INSTALL_MANIFEST_REL}"
    data = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            if fh.read() == data:
                return
        action = "overwrite"
    else:
        action = "create"
    out.write(_action(action, path))
    if not dry_run:
        _write_atomic(path, data)


def _install_profile_record(
    target: str, profile_spec: str, profile: Profile, dry_run: bool, out: TextIO
) -> None:
    """Record the profile for ``carcara run``. Gitignored on purpose: a custom
    profile is recorded as an absolute, machine-local path."""
    data = f"{_recorded_spec(profile_spec, profile)}\n".encode()
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
    prev_manifest = _load_manifest(target)
    # Installs made before the manifest existed don't know what they added:
    # leave them without one so uninstall uses its fallback instead.
    track = prev_manifest is not None or not os.path.exists(f"{target}/{INSTALLED_PROFILE_REL}")

    suffix = " (dry run)" if dry_run else ""
    out.write(f"carcara {__version__}: installing profile '{profile.name}' into {target}{suffix}\n")

    result = InstallResult()
    for rel, res in iter_claude_templates():
        dest = f"{target}/.claude/{rel}"
        if rel == SETTINGS_REL:
            action, data, _ = settings
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

    claude_md_mode = _install_claude_md(claude_md, profile, routing, dry_run, out)
    _install_carcara_gitignore(target, dry_run, out)
    _install_strict_flag(target, strict_policy, dry_run, out)
    _install_profile_record(target, profile_spec, profile, dry_run, out)
    if track:
        manifest = _build_manifest(
            prev_manifest,
            settings[0],
            settings[2],
            claude_md_mode,
            _recorded_spec(profile_spec, profile),
        )
        _install_manifest(target, manifest, dry_run, out)

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


# --- uninstall -------------------------------------------------------------------

UNINSTALL_USAGE = f"""carcara {__version__} - remove carcara from a project

Usage: carcara uninstall [options] [target-dir]

Removes the agents, commands, skill, settings entries, CLAUDE.md section and
.carcara files that `carcara install` added to <target-dir> (default: current
directory). Your own settings, edited agents/commands and run history are kept.

Options:
  -f, --force          also remove carcara agents and commands you edited
  -n, --dry-run        show what would be done without changing anything
      --purge          also delete the run history in .carcara/runs
  -h, --help           show this help
"""

# .carcara files owned by carcara (run history is handled separately).
CARCARA_FILES = (
    STRICT_POLICY_REL,
    ROUTING_OFF_REL,
    INSTALLED_PROFILE_REL,
    CARCARA_GITIGNORE_REL,
    INSTALL_MANIFEST_REL,
)
CLAUDE_DIRS = ("agents", "commands", "skills/carcara", "skills", "")


def _strip_claude_md(path: str, data: bytes, mode: str | None) -> bytes | None:
    """``data`` without the carcara block; None when the file should be deleted.

    ``mode`` is how install handled the file (manifest ``claude_md_mode``):
    ``append`` also drops the newline separator it added, ``create`` deletes
    an emptied file. Without a manifest (None) a blank line before the block is
    taken as the separator and an emptied file is deleted.
    """
    if count_markers(path, data) == 0:
        return data
    pieces = data.split(b"\n")
    begin, end = pieces.index(BEGIN_MARKER), pieces.index(END_MARKER)
    before = b"".join(p + b"\n" for p in pieces[:begin])
    after = b"\n".join(pieces[end + 1 :])
    if (mode == "append" and before.endswith(b"\n")) or (mode is None and before.endswith(b"\n\n")):
        before = before[:-1]
    new = before + after
    if not new and mode in ("create", None):
        return None
    return new


def _template_permissions() -> dict[str, list[str]]:
    res = templates_root().joinpath("claude", SETTINGS_REL)
    return json.loads(res.read_bytes().decode("utf-8")).get("permissions", {})


def unmerge_settings(
    settings: dict[str, Any], template: dict[str, list[str]], manifest: dict[str, Any] | None
) -> dict[str, Any]:
    """``settings`` (not modified) without what install merged into it.

    Carcara hook entries are always stripped. Permissions: with a manifest only
    the entries install added, else every ``template`` entry plus the legacy
    allow rules. ``model``: only when the manifest says install set it and it
    is unchanged. Lists, events and objects are dropped only when this emptied
    them and, if the manifest records ``settings_created_keys``, install
    created them.
    """
    created = manifest.get("settings_created_keys") if manifest else None
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if isinstance(hooks, dict):
        emptied = False
        for event in list(hooks):
            groups = hooks[event]
            kept = [g for g in map(_strip_carcara_hooks, groups) if g is not None]
            if groups and not kept and (created is None or event in created["hook_events"]):
                del hooks[event]
                emptied = True
            else:
                hooks[event] = kept
        if emptied and not hooks and (created is None or created["hooks"]):
            del out["hooks"]
    if manifest is None:
        remove = {k: list(v) for k, v in template.items()}
        remove.setdefault("allow", []).extend(LEGACY_ALLOW)
    else:
        remove = manifest["permissions_added"]
    perms = out.get("permissions")
    if isinstance(perms, dict):
        emptied = False
        for key, values in remove.items():
            current = perms.get(key)
            if not isinstance(current, list) or not current:
                continue
            kept = [v for v in current if v not in values]
            if kept or (created is not None and key not in created["permission_keys"]):
                perms[key] = kept
            else:
                del perms[key]
                emptied = True
        if emptied and not perms and (created is None or created["permissions"]):
            del out["permissions"]
    model_set = manifest.get("model_set") if manifest else None
    if model_set is not None and out.get("model") == model_set:
        del out["model"]
    return out


def _uninstall_profiles(target: str, manifest: dict[str, Any] | None) -> list[Profile]:
    """Profiles the installed files may have been rendered with: the recorded
    one (possibly a custom file) first, then every packaged profile."""
    specs: list[str] = []
    for spec in (manifest.get("profile") if manifest else None, read_installed_profile(target)):
        if spec and spec not in specs:
            specs.append(spec)
    for entry in sorted(profiles_root().iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".env") and entry.is_file():
            specs.append(entry.name[: -len(".env")])
    profiles = []
    for spec in specs:
        try:
            profiles.append(load_profile(spec))
        except (ProfileError, OSError):
            pass  # e.g. a custom profile file that was deleted since
    return profiles


def _uninstall_claude_files(
    target: str, profiles: list[Profile], force: bool, dry_run: bool, out: TextIO
) -> int:
    """Remove carcara's agents, commands and skill; returns the number removed."""
    removed = 0
    for rel, res in iter_claude_templates():
        dest = f"{target}/.claude/{rel}"
        if rel == SETTINGS_REL or not os.path.isfile(dest):
            continue
        if rel == SKILL_REL:
            if not _is_carcara_skill(dest):
                out.write(f"  keep       {dest} (not installed by carcara)\n")
                continue
        else:
            with open(dest, "rb") as fh:
                data = fh.read()
            if not force and data not in {_render_resource(res, p) for p in profiles}:
                out.write(f"  keep       {dest} (modified; use --force to remove)\n")
                continue
        out.write(_action("remove", dest))
        removed += 1
        if not dry_run:
            os.unlink(dest)
    return removed


def _prune_dirs(dirs: list[str]) -> None:
    """rmdir each directory that exists and is empty (never recursive)."""
    for d in dirs:
        try:
            os.rmdir(d)
        except OSError:
            pass


def _check_purge(target: str, runs: str) -> None:
    """Only a real directory inside ``target`` may be rmtree'd."""
    inside = os.path.join(os.path.realpath(target), RUNS_REL)
    if os.path.islink(runs) or not os.path.isdir(runs) or os.path.realpath(runs) != inside:
        raise InstallError(f"refusing to purge {runs}: not a directory inside {target}")


def uninstall(
    target: str = ".",
    *,
    dry_run: bool = False,
    force: bool = False,
    purge: bool = False,
    out: TextIO | None = None,
) -> int:
    """Remove what ``install`` added to ``target``; returns the number of changes.

    Raises InstallError before any change (bad CLAUDE.md markers, bad
    settings.json or manifest, home directory, unsafe --purge path).
    """
    out = sys.stdout if out is None else out
    target = target or "."
    if not os.path.isdir(target):
        raise InstallError(f"target directory does not exist: {target}")
    if _is_user_config_dir(target):
        raise InstallError(
            "refusing to uninstall from your home directory (Claude Code loads it as "
            "user-level config for every project); pass a project directory"
        )
    for rel in CARCARA_FILES:
        path = f"{target}/{rel}"
        if os.path.isdir(path) and not os.path.islink(path):
            raise InstallError(f"{path} is a directory, not a carcara file; refusing to uninstall")
    if purge:
        holder = runstore.live_lock_holder(f"{target}/{ACTIVE_RUN_REL}")
        if holder is not None:
            raise InstallError(f"refusing to purge: run {holder['run_id']} is active")
    manifest = _load_manifest(target)
    claude_md = f"{target}/CLAUDE.md"
    claude_md_new: bytes | None = None
    claude_md_data = None
    if os.path.isfile(claude_md):
        with open(claude_md, "rb") as fh:
            claude_md_data = fh.read()
        claude_md_new = _strip_claude_md(
            claude_md, claude_md_data, manifest.get("claude_md_mode") if manifest else None
        )
    settings_dest = f"{target}/.claude/{SETTINGS_REL}"
    settings = _load_settings(settings_dest) if os.path.exists(settings_dest) else None
    runs = f"{target}/{RUNS_REL}"
    if purge and os.path.lexists(runs):
        _check_purge(target, runs)

    suffix = " (dry run)" if dry_run else ""
    out.write(f"carcara {__version__}: uninstalling from {target}{suffix}\n")
    changes = _uninstall_claude_files(
        target, _uninstall_profiles(target, manifest), force, dry_run, out
    )

    if settings is not None:
        raw, data = settings
        new = unmerge_settings(data, _template_permissions(), manifest)
        if new != data:
            if manifest is None:
                out.write(
                    "warning: no install manifest; removing every carcara template "
                    "permission (also ones you added yourself) and keeping 'model'\n"
                )
            changes += 1
            created = manifest.get("settings_created") if manifest else True
            if not new and created:
                out.write(_action("remove", settings_dest))
                if not dry_run:
                    os.unlink(settings_dest)
            elif _dump_settings(new) != raw:
                out.write(_action("strip", settings_dest))
                if not dry_run:
                    _write_atomic(settings_dest, _dump_settings(new))
    if not dry_run:
        _prune_dirs([os.path.join(target, ".claude", d) for d in CLAUDE_DIRS])

    if claude_md_data is not None and claude_md_new != claude_md_data:
        changes += 1
        if claude_md_new is None:
            out.write(_action("remove", claude_md))
            if not dry_run:
                os.unlink(claude_md)
        else:
            out.write(_action("strip", claude_md))
            if not dry_run:
                with open(claude_md, "wb") as fh:
                    fh.write(claude_md_new)

    # install.json last: if anything fails before it, uninstall can be retried.
    for rel in CARCARA_FILES:
        path = f"{target}/{rel}"
        if os.path.lexists(path):
            out.write(_action("remove", path))
            changes += 1
            if not dry_run:
                os.unlink(path)
    if os.path.lexists(runs):
        if purge:
            out.write(_action("purge", runs))
            changes += 1
            if not dry_run:
                shutil.rmtree(runs)
        else:
            out.write(f"  keep       {runs} (run history; use --purge to delete)\n")
    if purge:
        active = f"{target}/{ACTIVE_RUN_REL}"
        leftovers = sorted(
            path
            for pattern in runstore.LOCK_LEFTOVER_GLOBS
            for path in glob.glob(os.path.join(glob.escape(f"{target}/.carcara"), pattern))
        )
        for path in ([active] if os.path.lexists(active) else []) + leftovers:
            out.write(_action("purge", path))
            changes += 1
            if not dry_run:
                os.unlink(path)
    if not dry_run:
        _prune_dirs([f"{target}/.carcara"])

    if changes:
        out.write(f"done: {changes} item(s) removed or stripped.\n")
    else:
        out.write("nothing to uninstall\n")
    return changes


def uninstall_main(argv: list[str]) -> int:
    """``carcara uninstall`` argument handling, in the style of ``main``."""
    target = ""
    force = dry_run = purge = False
    args = list(argv)
    try:
        while args:
            arg = args[0]
            if arg in ("-f", "--force"):
                force = True
            elif arg in ("-n", "--dry-run"):
                dry_run = True
            elif arg == "--purge":
                purge = True
            elif arg in ("-h", "--help"):
                sys.stdout.write(UNINSTALL_USAGE)
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
        uninstall(target or ".", dry_run=dry_run, force=force, purge=purge)
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
