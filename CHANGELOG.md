# Changelog

## 0.2.0

### Added
- `carcara run "<task>"`: headless orchestrator for the SDLC pipeline built on
  the Claude Agent SDK, with an approval gate, fix loop, budget cap, resumable
  runs stored in `.carcara/runs/<id>/` and a per-role tool policy.
- `carcara run` uses your Claude subscription login by default:
  `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are hidden from the `claude`
  CLI unless `--use-api-key` is passed (pay-per-token API billing). Reported
  costs are labelled as estimates.
- `carcara profiles` subcommand: list profiles and their model routing.
- pytest suite (including frozen 0.1.0 bash parity fixtures) and ruff config.

### Changed
- carcara is now a Python package (>=3.10) installed with `pipx install .` or
  `uv tool install .`; `carcara install` keeps byte-for-byte parity with 0.1.0.
- `install`, `profiles` and `run` as the first argument are now subcommands.
  To target a directory with one of those names, use `carcara -- <dir>` or
  `carcara install <dir>`.
- `-h` prints argparse help.
- The version is 0.2.0.
- Profiles and templates moved to `src/carcara/data/`.
- `tests/run.sh` has been replaced by pytest.

### Deprecated
- Running the bash `bin/carcara` from a git clone. It still works but will be
  removed in 0.3.0.
