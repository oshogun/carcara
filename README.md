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

Requirements: `bash`, `sed`, `awk`, `find` (macOS and Linux).

```sh
git clone https://github.com/oshogun/carcara.git
ln -s "$PWD/carcara/bin/carcara" /usr/local/bin/carcara   # optional
```

## Usage

```sh
carcara [options] [target-dir]     # target-dir defaults to the current directory
```

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

Profiles live in `profiles/*.env`. To customise, copy one and pass its path:

```sh
cp profiles/balanced.env my.env    # edit MODEL_* values (opus, sonnet, haiku, inherit or a model id)
carcara --profile ./my.env --force .
```

## Development

```sh
tests/run.sh                       # test suite
shellcheck bin/carcara tests/run.sh
```
