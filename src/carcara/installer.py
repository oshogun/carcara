"""Install carcara's agents, commands, settings and CLAUDE.md section.

Semantics (messages, ordering, dry-run, skip/--force, CLAUDE.md markers) match
the 0.1.0 bash installer byte for byte, except the version string.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

from carcara import __version__
from carcara.profiles import Profile, ProfileError, list_profiles, load_profile
from carcara.resources import claude_md_template, iter_claude_templates, render, templates_root

BEGIN_MARKER = b"<!-- carcara:begin -->"
END_MARKER = b"<!-- carcara:end -->"

USAGE = f"""carcara {__version__} - agentic SDLC framework for Claude Code

Usage: carcara [options] [target-dir]

Installs subagents, slash commands, settings and a CLAUDE.md section into
<target-dir> (default: current directory).

Options:
  -p, --profile NAME   model profile: economy | balanced | quality, or a path
                       to a custom profile file (default: balanced)
  -f, --force          overwrite existing carcara files in .claude/
  -n, --dry-run        show what would be done without writing anything
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


def _claude_md_block(profile: Profile) -> bytes:
    return (
        BEGIN_MARKER + b"\n" + _render_resource(claude_md_template(), profile) + END_MARKER + b"\n"
    )


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


def _install_claude_md(dest: str, profile: Profile, dry_run: bool, out: TextIO) -> None:
    block = _claude_md_block(profile)
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


def install(
    target: str = ".",
    profile_spec: str = "balanced",
    *,
    force: bool = False,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> InstallResult:
    """Install into ``target``. Raises InstallError/ProfileError before any write."""
    out = sys.stdout if out is None else out
    target = target or "."
    if not templates_root().joinpath("claude").is_dir():
        raise InstallError(f"templates not found in {templates_root()}")
    if not os.path.isdir(target):
        raise InstallError(f"target directory does not exist: {target}")
    profile = load_profile(profile_spec)
    claude_md = f"{target}/CLAUDE.md"
    if os.path.exists(claude_md):
        with open(claude_md, "rb") as fh:
            count_markers(claude_md, fh.read())

    suffix = " (dry run)" if dry_run else ""
    out.write(f"carcara {__version__}: installing profile '{profile.name}' into {target}{suffix}\n")

    result = InstallResult()
    for rel, res in iter_claude_templates():
        dest = f"{target}/.claude/{rel}"
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
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(_render_resource(res, profile))

    _install_claude_md(claude_md, profile, dry_run, out)

    out.write(f"done: {result.written} file(s) written, {result.skipped} skipped.\n")
    if result.skipped > 0:
        out.write(
            "note: skipped files keep their previous content; re-run with --force "
            f"to apply profile '{profile.name}' to them.\n"
        )
    out.write(f"Next: start Claude Code in {target} and run: /sdlc <task>\n")
    return result


def main(argv: list[str]) -> int:
    """``carcara install`` argument handling, mirroring the bash ``case`` loop."""
    profile = "balanced"
    target = ""
    force = False
    dry_run = False
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
        install(target or ".", profile, force=force, dry_run=dry_run)
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
