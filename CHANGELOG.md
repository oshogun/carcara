# Changelog

## Unreleased

### Changed
- The PyPI distribution is now named `carcara-sdlc` (`carcara` is taken on
  PyPI): install with `pipx install carcara-sdlc`. The import package and the
  `carcara` command are unchanged.
- The test stage no longer fails the run when it runs out of turns (#18).
  Its default turn limit goes from 30 to 50, and the prompt tells the
  test-runner to run each command once and report without debugging. When
  it still hits the limit it is retried once and told to report what it has.
  A second time stops the run as `needs_human`, not `failed`. Other stages
  are unchanged.
- Feedback given with `carcara resume --reject --feedback` now also reaches
  the implement prompts (each L step and the M implement), not only the
  revised plan (#20). All feedback entries are included, latest last.

### Added
- `carcara uninstall [target] [-n] [-f] [--purge]` removes carcara's agents,
  commands, skill, `settings.json` entries, `CLAUDE.md` section and
  `.carcara/` files while keeping user content; install then uninstall
  restores the original files (#9). Install now records what it added in
  `.carcara/install.json`.
- implementer and test-runner Bash now goes through a best-effort deny-list.
  It blocks `git push`, `git reset --hard`, `git clean -f`, curl/wget
  fetch-and-exec, shell access to secret paths, and shell writes outside the
  repo or into `.git`/`.claude`/`.carcara`. Pass
  `carcara run --unrestricted-bash` to disable it for one invocation; it
  prints a warning and is not persisted across `--resume`. A stage cannot
  enable the opt-out (#6).
- The Bash deny-list skips the values of wrapper options (`sudo -u`,
  `nice -n`, `env -u`/`-C`, `timeout -s`/`-k`, `xargs -n`/`-I`, ...) and
  splits `env -S` strings, so commands like `sudo -u root git push` or
  `nice -n 5 git push` no longer get past it.
- Verifiability gate: `carcara run` stops at the approval gate when an M/L
  plan's step files, or an S run's changed files, match a low-verifiability
  pattern. The defaults are `.github/**`, `**/migrations/**`, `**/auth/**`,
  `**/policy*` and `**/policy/**`. S runs are gated after implement and before
  test/review, on the files git shows as changed since the run base (plus any
  the implementer reported).
  `state.json` records `gate: {trigger, paths, stage}`, where the trigger is
  `size`, `flag`, `revision` or `verifiability`.
- Optional project config `.carcara/config.json` with `verifiability_paths`
  (replaces the defaults; `[]` turns the trigger off) and `probes`. A run
  snapshots it at start and `--resume` uses the snapshot; agents cannot write
  it.
- The reviewer's structured output now requires an `unverified` list with at
  most 20 items. Each item is `{id, kind, text}`: kind is `external`,
  `normative` or `untested`, and text is at most 200 characters. carcara
  assigns stable ids (`U1`, `U2`, ...) within a run; an item whose text
  changes gets a new id. Open items, with counts
  by kind, are shown in the report and `carcara status`.
- Probes: allow-listed, unauthenticated HTTP GET checks such as
  `"pypi-name": "https://pypi.org/pypi/{arg}/json"`. carcara runs them after
  the review for the `external` items that reference them (5 s timeout) and
  records the item id, the probe `{name, arg, expect}` and a short result,
  re-running the probe when it changes. Redirects are not followed and proxy
  environment variables are ignored. The agent Bash policy is unchanged.
- Extent facts in the state, the report and `carcara status`: files changed,
  top-level areas (at most 10) and fix rounds, tagged with the rule version
  `carcara/extent-1`.
- `carcara run --issue N [--repo owner/name]` takes the task from GitHub issue
  N via `gh` (the repo defaults to the `origin` remote); task text given as
  well is appended as additional instructions. `--no-urutau` turns off Urutau
  reporting for the invocation.
- Triage also outputs a size range (`S`, `M`, `L`, `S-M`, `M-L`, `S-L`) and the
  main uncertainty kind (`external`, `normative`, `untested`, `none`). Both
  are stored in the state and shown in the report and `carcara status`; they
  are null when `--size` forced the size.
- Urutau reporting for `--issue` runs when `URUTAU_MCP_TOKEN` (or
  `~/.config/carcara/urutau.json`) provides a token: `record_run` claims the
  card at start, renews it with a 10-minute heartbeat, and reports pauses,
  resumes and the end, with the unverified inventory, confirmed probes and
  findings at pauses and the end. The card estimate is read at start. A card
  claimed by another run stops the run before any work; other Urutau failures
  after the start only add a warning event. Each call uses its own MCP
  session, and the token is only ever sent to `<base>/mcp` (redirects are not
  followed with it). Interactive gates send `awaiting_approval` while they
  wait; resuming a run Urutau already saw end reports under `<run id>-rN`.
  See "Urutau integration" in the README.

### Fixed
- `carcara run` now defaults to the profile chosen at `carcara install`
  (recorded in `.carcara/profile`) instead of always using balanced; an
  unloadable recorded profile warns and falls back to balanced. The run start
  line shows the profile: `carcara: run <id> started (profile <name>)` (#16).
- A stage that ends without structured output is retried once, with a nudge
  to emit its result, before the run fails. Both attempts' costs are counted
  (#15).

### Removed
- The deprecated bash `bin/carcara` (deprecated in 0.2.0). Install the Python
  package with `pipx install .` or `uv tool install .` instead.

## 0.3.0

### Added
- Automatic routing of code-change requests in Claude Code: `carcara install`
  adds a `carcara` skill, a routing rule in the `CLAUDE.md` section and
  `PreToolUse` / `UserPromptSubmit` hooks in `.claude/settings.json`. Just ask
  for a change; Claude Code hands it to `carcara run` and reports back.
- The `carcara hook` PreToolUse handler is the sole approver of carcara
  commands: it allows only the exact `carcara run|status|diff` forms the skill
  uses and asks you for everything else. `--yes`, `--accept-failures`,
  `--use-api-key`, `--max-budget-usd` and resuming a run that hit its budget
  always need your confirmation.
- Main-session edit guard: while routing is on, the main session cannot edit
  project files directly, and no session (subagents included) can write
  `.claude/settings*.json` or `.claude/skills/carcara/`. Opt-outs:
  `carcara routing off|on|status`, `CARCARA_OFF=1`, `carcara install --no-routing`.
- `carcara install --strict-policy`: also apply the carcara tool policy to
  carcara-role subagents in interactive sessions.
- Dirty-tree runs: `carcara run --allow-dirty` diffs against a snapshot of
  your uncommitted work, so it is not attributed to carcara.
- `carcara diff [RUN_ID] [--stat]` and `carcara status [RUN_ID] [--json|--plan]`.
- Single active run lock; `carcara run` exits 6 when another run is active.
- `carcara run --resume <id> --reject` / `--feedback TEXT|-` (re-plan or guide
  a needs_human retry), `--accept-failures`, and `carcara run -` (task on stdin).
- The run budget (`--max-budget-usd`) is stored with the run and kept on
  `--resume` unless a new value is given.
- Under Claude Code (`CLAUDECODE` set) the approval gate never waits on stdin.
- Nested-run guard: `carcara run` refuses to start inside a carcara stage.
- Abbreviated long options (e.g. `--ye`) are rejected.
- `carcara install` refuses to install into your home directory, the Claude
  config dir (`CLAUDE_CONFIG_DIR` or `~/.claude`) or anything inside
  `~/.claude`, which Claude Code would load as user-level config.

### Changed
- `.claude/settings.json` is now merged into an existing file (your
  permissions, hooks and model are kept) instead of skipped or overwritten.
- `carcara install` output differs from 0.2.0: the skill, the hooks and the
  routing text in `CLAUDE.md`. `carcara install --no-routing` reproduces the
  0.2.0 `settings.json` and `CLAUDE.md`.
- The interactive `/sdlc` S path now delegates the change to the `implementer`
  subagent instead of editing in the main session.
- The version is 0.3.0.

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
