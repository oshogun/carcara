# Golden installer fixtures

Byte-exact output of the 0.1.0 bash installer (`bin/carcara`), used to check
that the Python installer produces identical files.

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

## Frozen bash parity

`tests/fixtures/bash_parity.json` records exit code, stdout, stderr and the
resulting tree (sha256 per file) of the retired 0.1.0 bash installer for a set
of CLI scenarios; `test_parity_with_bash` checks the Python CLI against it.
It cannot be regenerated (the bash installer is gone): if templates change,
update or drop the affected `tree_sha256` entries deliberately.

0.2.0: the `CLAUDE.md` template gained one line mentioning `carcara run`, so
the `CLAUDE.md` hashes in `bash_parity.json` were replaced deliberately with
the Python installer's new output (all other entries are unchanged 0.1.0 values).
