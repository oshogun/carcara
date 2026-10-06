# carcara
An agentic SDLC framework for Claude Code.

`carcara` sets up [Claude Code](https://docs.claude.com/en/docs/claude-code)
in any directory with a team of specialised subagents, SDLC slash commands and
project instructions. Each agent is pinned to the cheapest model that does its
job well, so you get most of the quality of running everything on the biggest
model with maximum thinking ("ultracode"), at a fraction of the token cost.

## Why

Running a single top-tier model with extended thinking for an entire session
spends premium tokens on everything — grepping, reading logs, running tests,
writing docs — and keeps all of that output in one ever-growing context.
carcara balances this by:

- **Model routing** — Opus only where reasoning pays off (planning), Sonnet
  for orchestration, coding and review, Haiku for search, test runs and docs.
- **Context isolation** — verbose work (code search, build/test logs) happens
  inside subagents, which return compact, fixed-format summaries.
- **Triage** — `/sdlc` sizes every task (S/M/L) and only spins up the
  explorer/architect/reviewer when the task warrants it.
- **Bounded loops** — at most two fix iterations before asking you.
- **Targeted thinking** — extended thinking is reserved for design questions.

## Install

Requirements: Python 3.10 or newer.

```sh
git clone https://github.com/oshogun/carcara.git && cd carcara
pipx install .          # or: uv tool install .
# or straight from git: pipx install git+https://github.com/oshogun/carcara.git
```

Running `bin/carcara` from a git clone still works but is deprecated and will
be removed in 0.3.0.

## Usage

```sh
carcara [install] [options] [target-dir]   # target-dir defaults to the current directory
carcara profiles                           # list profiles
carcara run "<task>"                       # run the pipeline headlessly (see below)
```

`install`, `profiles` and `run` as the first argument are subcommands; to
target a directory with one of those names use `carcara -- <dir>` or
`carcara install <dir>`.

| Option | Description |
|---|---|
| `-p, --profile NAME` | `economy`, `balanced` (default), `quality`, or a path to a custom profile file |
| `-f, --force` | overwrite existing carcara files in `.claude/` |
| `-n, --dry-run` | show what would be done without writing |
| `-l, --list-profiles` | list profiles and their model routing |
| `-V, --version` / `-h, --help` | version / help |

Then start Claude Code in the target directory and run:

```
/sdlc add rate limiting to the public API
```

### What gets installed

```
<target>/
├── CLAUDE.md                     # managed section between carcara markers
└── .claude/
    ├── settings.json             # main-session model + safe permissions
    ├── agents/
    │   ├── explorer.md           # read-only code search → file:line findings
    │   ├── architect.md          # plans non-trivial changes
    │   ├── implementer.md        # writes code + tests
    │   ├── test-runner.md        # runs build/lint/tests, summarises failures
    │   ├── reviewer.md           # high-signal diff review
    │   └── doc-writer.md         # updates affected docs
    └── commands/
        ├── sdlc.md               # /sdlc <task>: triaged full pipeline
        ├── sdlc-plan.md          # /sdlc-plan <task>: explore + plan only
        ├── sdlc-build.md         # /sdlc-build [plan]: implement + test
        ├── sdlc-test.md          # /sdlc-test [scope]
        └── sdlc-review.md        # /sdlc-review [focus]
```

`settings.json` denies the `Read` tool on `.env*` and `secrets/**`. This is a
guard rail, not a sandbox: agents with `Bash` could still read such files via
shell commands, so keep real secrets out of the working tree or add your own
`Bash(...)` deny rules.

Re-running is safe: existing files in `.claude/` are skipped unless
`--force` is given, and an existing `CLAUDE.md` keeps its content — carcara
only appends or updates the section between `<!-- carcara:begin -->` and
`<!-- carcara:end -->`.

### Pipeline (`/sdlc`)

| Size | Flow |
|---|---|
| S | main session edits directly → test-runner (→ reviewer if security, data handling or public APIs are touched) |
| M | explorer → implementer → test-runner → reviewer |
| L | explorer → architect → **your approval** → implementer → test-runner → reviewer → doc-writer |

## carcara run

`carcara run "<task>"` runs the same pipeline headlessly from the terminal,
one Claude Agent SDK session per stage.

Requirements: the [Claude Code CLI](https://code.claude.com/docs/en/setup) on
`PATH` and authenticated.

**Billing.** Each stage runs through the `claude` CLI using your Claude Code
login, so by default runs draw on your Claude subscription (Pro/Max) usage
limits. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are hidden from the CLI
(with a notice) unless you pass `--use-api-key`, which bills the pay-per-token
API instead; on `--resume` the run keeps the choice it was started with.
With `--project-settings`, the project's `.claude/settings.json` is loaded too, and
an `env.ANTHROPIC_API_KEY` or `apiKeyHelper` there can still switch the CLI to
API billing.
Explicit provider settings (`CLAUDE_CODE_USE_BEDROCK`, `..._VERTEX`,
`..._FOUNDRY`) are left alone. Reported costs are the SDK's estimate at API
prices: on a subscription they are not charged, but they approximate how much
plan usage the run consumed.

```sh
carcara run "add rate limiting to the public API"
carcara run --dry-run                 # print the stage table, no backend calls
```

Stages per size (triage picks the size unless `--size S|M|L` is given):

| Size | Stages |
|---|---|
| S | implement → test (→ review with `--review`) |
| M | explore → plan → implement → test → review |
| L | explore → plan (architect) → **approval** → implement per plan step → test → review → docs |

**Approval gate.** L plans (and M plans with `--approve-plan`) wait for you. On
a TTY you are prompted; `--yes` auto-approves; without a TTY the run stops with
exit code 3 and is continued later with `--resume <id> --yes`. If tests still
fail, the fix loop runs at most 2 iterations, then exits with code 4.

| Option | Description |
|---|---|
| `-p, --profile NAME` | profile name or `.env` path (default `balanced`) |
| `--max-budget-usd USD` | cap the total estimated cost (exit 5 when exceeded); on a subscription this is a proxy for plan usage |
| `--use-api-key` | let the CLI use `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` (pay-per-token API billing) instead of your subscription login |
| `--dry-run` | show stages, models and tools without calling Claude |
| `--plan-only` | stop after the plan |
| `--resume RUN_ID` / `--list` | continue a stored run / list stored runs |
| `--allow-dirty` | allow uncommitted changes (clean tree required by default) |
| `--project-settings` | load the project's Claude Code settings and CLAUDE.md (note: their `env` / `apiKeyHelper` can re-enable API billing) |
| `--cwd DIR` | project directory (default `.`) |

The budget is checked from the estimated stage costs the SDK reports; a stage
that errors out reports none, so its usage may go uncounted.

**Run directory.** Each run is stored in `.carcara/runs/<id>/`: `state.json`,
`events.jsonl` (append-only log) and `report.md`. The base commit is recorded
in the state.

| Exit code | Meaning |
|---|---|
| 0 | done (or plan-only) |
| 1 | error |
| 3 | awaiting plan approval |
| 4 | needs human (e.g. tests still failing after the fix loop) |
| 5 | budget exceeded |
| 130 | interrupted (state saved; resume with `--resume`) |

### Tool policy

Each role gets only its own tools. Read-only roles (explorer, reviewer) may run
Bash only from a flag allowlist (git status/diff/log/show, ls, rg, grep, find,
cat, wc, head, tail; no shell metacharacters, no unknown options). Reads are
confined to the repo, and `.env`, `.env.*` and `secrets/` are refused (case-
insensitively, symlinks resolved). Write tools are confined to the repo and
refuse `.git`, `.claude` and `.carcara`. The policy is
enforced by a PreToolUse hook, which applies in every permission mode;
`bypassPermissions` is never used.

Known risks:

- implementer and test-runner have unrestricted Bash. This is mitigated by the
  clean-tree requirement and the base commit recorded for each run.
- Grep and recursive searches may still surface secrets in a searched directory.
- `setting_sources` is empty by default, so project settings and CLAUDE.md are
  not loaded into stages; pass `--project-settings` to opt in.

## Profiles

| Role | economy | balanced | quality |
|---|---|---|---|
| main session | sonnet | sonnet | opus |
| architect | sonnet | opus | opus |
| implementer | sonnet | sonnet | opus |
| reviewer | haiku | sonnet | opus |
| explorer | haiku | haiku | sonnet |
| test-runner | haiku | haiku | haiku |
| doc-writer | haiku | haiku | sonnet |

Profiles live in `src/carcara/data/profiles/*.env`. To customise, copy one and
pass its path (also accepted by `carcara run -p`):

```sh
cp src/carcara/data/profiles/balanced.env my.env    # edit MODEL_* values (opus, sonnet, haiku, inherit or a model id)
carcara --profile ./my.env --force .
```

## Development

```sh
pip install -e '.[dev]'
ruff check .
pytest -q
tests/fixtures/regen_golden.sh     # regenerate golden installer output after template/profile changes
```

See `tests/fixtures/golden/README.md` for the golden and bash-parity fixtures.
