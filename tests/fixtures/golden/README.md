# Golden installer fixtures

Byte-exact output of the installer for a set of cases. They started as the
output of the 0.1.0 bash installer (since removed); since 0.3.0 they are the
Python installer's output (routing skill, settings hooks, `{{ROUTING}}` text)
and are regenerated deliberately whenever templates or profiles change.

## Layout

One directory per case:

| Case | Installer input |
|---|---|
| `economy`, `balanced`, `quality` | `-p <name>` into an empty directory |
| `custom` | `-p tests/fixtures/custom.env` (distinct model per key) |
| `claude-md-append` | `-p balanced`; target already has a `CLAUDE.md` without carcara markers |
| `claude-md-update` | `-p balanced`; target already has a `CLAUDE.md` with a stale carcara block between user content |

Files are renamed so Claude Code never picks them up as live config:

- `dot-claude/` is the installed `.claude/` directory.
- `CLAUDE.md.golden` is the resulting `CLAUDE.md`.
- `CLAUDE.md.input` is the pre-existing `CLAUDE.md` (append/update cases;
  sources in `tests/fixtures/inputs/`).

## Regenerating

    tests/fixtures/regen_golden.sh

By default this runs the Python CLI from this checkout (`python3 -m carcara`
with `src/` on `PYTHONPATH`); set `CARCARA=<command>` to run a different
installer. Regenerate whenever the
templates or profiles change, and review the diff before committing.

## Install snapshot

`tests/fixtures/install_snapshot.json` (formerly `bash_parity.json`) records
exit code, stdout, stderr and the resulting tree (sha256 per file) of the
installer for a set of CLI scenarios; `test_install_snapshot` checks the CLI
against it. It started as a frozen recording of the retired 0.1.0 bash
installer. 0.2.0 replaced the `CLAUDE.md` hashes (one line about `carcara
run`); 0.3.0 regenerated the success cases from the Python installer (new
`skills/carcara/SKILL.md`, settings.json hooks and permissions, routing text in
`CLAUDE.md`, S-size delegation in `sdlc.md`, new final hint). The error cases
and `-l` output are unchanged 0.1.0 values. The uninstall work (#9) added
the `.carcara/install.json` manifest line and hash to the success cases. Regenerate it with a script that
replays each scenario, and review the diff before committing.
