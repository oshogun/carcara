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
pipx install carcara-sdlc    # or: uv tool install carcara-sdlc
```

For the latest unreleased code, install from source:

```sh
git clone https://github.com/oshogun/carcara.git && cd carcara
pipx install .          # or: uv tool install .
# or straight from git: pipx install git+https://github.com/oshogun/carcara.git
```

## Using carcara from Claude Code

After `carcara install` (and `carcara` on your `PATH`), start Claude Code in
the project and **just ask for a change** ("add rate limiting to the public
API"). No slash command is needed.

What happens:

- The `CLAUDE.md` routing rule and the `carcara` skill hand code-change
  requests to `carcara run`, which runs in the background (explore → plan →
  implement → test → review, one Agent SDK session per stage). Questions and
  explanations are answered normally.
- The main session cannot edit project files itself (a hook blocks it), and no
  session can change `.claude/settings*.json` or `.claude/skills/carcara/`.
- When the run finishes, Claude summarises the result and points you to
  `carcara diff <id>` / `carcara status <id>`. Paused runs (plan approval,
  failing tests, budget) are listed in each prompt's context and offered for
  resume.

Approvals: carcara's PreToolUse hook is the only approver of carcara commands.
It allows the exact `carcara run|status|diff` forms the skill uses and asks you
for every other command that invokes carcara. Approving a plan (`--yes`),
accepting failures (`--accept-failures`), API billing (`--use-api-key`),
changing the budget (`--max-budget-usd`) and resuming a run that hit its budget
always show you a permission prompt; that prompt is your decision.

Opt-outs:

- `carcara routing off` / `carcara routing on` / `carcara routing status`
  (per project, a `.carcara/routing-off` flag file);
- `CARCARA_OFF=1` in the environment (per session);
- `carcara install --no-routing` (no skill, no hooks; `settings.json` and
  `CLAUDE.md` as installed by 0.2.0).

Trust: interactive Claude Code asks you to trust the folder on first start, and
project permission rules apply only after that. carcara's own commands do not
depend on trust (the hook approves them), but a malicious repository can ship
its own `.claude/` config, so only trust folders you would run code from.
`claude -p` works too.

The `/sdlc*` slash commands stay available as an optional manual path.

## Usage

```sh
carcara [install] [options] [target-dir]   # target-dir defaults to the current directory
carcara uninstall [options] [target-dir]   # remove what install added (see below)
carcara profiles                           # list profiles
carcara run "<task>"                       # run the pipeline headlessly (see below)
carcara status [RUN_ID] [--json|--plan]    # show a run (default: active, else latest)
carcara diff [RUN_ID] [--stat]             # a run's changes since its base (secrets excluded)
carcara routing on|off|status              # Claude Code routing for this project
```

`install`, `uninstall`, `profiles`, `run`, `status`, `diff`, `routing` and `hook` as the
first argument are subcommands; to
target a directory with one of those names use `carcara -- <dir>` or
`carcara install <dir>`.

| Option | Description |
|---|---|
| `-p, --profile NAME` | `economy`, `balanced` (default), `quality`, or a path to a custom profile file |
| `-f, --force` | overwrite existing carcara files in `.claude/` |
| `-n, --dry-run` | show what would be done without writing |
| `--no-routing` | don't route code changes through `carcara run` (no skill or hooks; removes them) |
| `--strict-policy` | also apply the carcara tool policy to carcara subagents in interactive sessions |
| `-l, --list-profiles` | list profiles and their model routing |
| `-V, --version` / `-h, --help` | version / help |

Then start Claude Code in the target directory and ask for a change (see
[Using carcara from Claude Code](#using-carcara-from-claude-code)), or run
`/sdlc add rate limiting to the public API` explicitly.

### What gets installed

```
<target>/
├── CLAUDE.md                     # managed section between carcara markers
└── .claude/
    ├── settings.json             # main-session model, safe permissions, routing hooks (merged)
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
    └── skills/carcara/SKILL.md   # routing skill (omitted with --no-routing)
```

`settings.json` denies the `Read` tool on `.env*` and `secrets/**`. This is a
guard rail, not a sandbox: agents with `Bash` could still read such files via
shell commands, so keep real secrets out of the working tree or add your own
`Bash(...)` deny rules.

Re-running is safe: existing files in `.claude/` are skipped unless
`--force` is given; an existing `settings.json` is merged (your permissions,
hooks and model are kept; carcara's hook entries are replaced); and an
existing `CLAUDE.md` keeps its content — carcara
only appends or updates the section between `<!-- carcara:begin -->` and
`<!-- carcara:end -->`.

### Uninstalling

`carcara uninstall [target-dir]` removes what `carcara install` added: the
carcara agents, commands and `carcara` skill, carcara's hooks, permissions and
`model` in `settings.json`, the marked `CLAUDE.md` section and carcara's files
in `.carcara/`. Files and empty directories that install created are deleted;
install followed by uninstall gives back the original files (an existing
`settings.json` is rewritten as 2-space-indented JSON if anything in it changed).

| Option | Description |
|---|---|
| `-n, --dry-run` | show what would be removed without changing anything |
| `-f, --force` | also remove carcara agents and commands you edited |
| `--purge` | also delete the run history in `.carcara/runs` |

Kept: agents and commands you edited (`keep ... (modified)`, unless `-f`), a
`carcara` skill that carcara didn't write, your own `settings.json` entries
(including permissions identical to carcara's that you had before installing,
and a `model` you changed), and `.carcara/runs` unless `--purge`. Like
install, it refuses to run in your home directory or `~/.claude`.

Install records what it added in `.carcara/install.json`. Projects installed
before that file existed have no record: uninstall then removes every
permission from carcara's template (also identical ones you added yourself;
it prints a warning), keeps `model`, and treats a blank line before the
`CLAUDE.md` section as the separator install added.

### Pipeline (`/sdlc`)

| Size | Flow |
|---|---|
| S | implementer → test-runner (→ reviewer if security, data handling or public APIs are touched) |
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
API instead; on `--resume` the run keeps the choice it was started with, unless
`--use-api-key` is passed (the run then stays on API billing).
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
| S | implement (→ **approval** if low-verifiability paths changed) → test (→ review with `--review`) |
| M | explore → plan (→ **approval** with `--approve-plan` or low-verifiability paths) → implement → test → review |
| L | explore → plan (architect) → **approval** → implement per plan step → test → review → docs |

**Approval gate.** The run waits for you when one of these triggers fires:

| Trigger | When |
|---|---|
| `size` | every L plan |
| `flag` | M plans with `--approve-plan` |
| `revision` | a plan re-made from `--feedback` |
| `verifiability` | an M/L plan step's files, or (for S) the files changed since the run base, match a low-verifiability path pattern |

The default patterns are `.github/**`, `**/migrations/**`, `**/auth/**`,
`**/policy*` and `**/policy/**`. To change them, see [Project config](#project-config-carcaraconfigjson).
S runs have no plan, so the verifiability gate runs after implement and before
test/review. It checks the files git shows as changed since the run base, plus
any the implementer reported, so an omitted file still gates. The changes are
then already in the working tree (uncommitted;
inspect them with `carcara diff <id>`). Approving continues with test and
review. Rejecting fails the run but does not revert the changes. `--feedback`
is refused at this gate. The same post-implement check also catches M/L
changes that touch a pattern even though no plan step listed it. The gate
prompt starts with `Gate: <reason>`, and `state.json` records
`gate: {trigger, paths, stage}`, where `stage` is `plan` or `post-implement`.

On a TTY you are prompted; `--yes` auto-approves; without a TTY the run stops with
exit code 3 and is continued later with `--resume <id> --yes`. If tests still
fail, the fix loop runs at most 2 iterations, then exits with code 4.

**Unverified assumptions.** The reviewer must list in `unverified` what its
verdict relies on but cannot check: claims about systems outside the repo
(`external`, e.g. a package name being free or a secret being configured),
interpretations of the request (`normative`), and behaviour no test exercises
(`untested`). The list has at most 20 items of at most 200 characters each.
When the changed files match a low-verifiability pattern, the prompt adds
path-specific questions. For `.github/**`, for example, it asks which accounts,
package names, environments or secrets the change assumes. carcara assigns the
ids (`U1`, `U2`, ...), and they stay stable across fix rounds; an item whose
text changes gets a new id. When
[probes](#project-config-carcaraconfigjson) are configured, an `external` item
may name one. carcara runs it after the review and runs it again when the
item's probe (name, arg or expect) changes. An item whose current probe result
confirms the expectation is marked resolved.

**Report fields.** Besides files, tests, review and cost, `report.md` and
`carcara status` show:

- `extent: N files, areas a, b, fix rounds K [carcara/extent-1]`: the raw
  extent facts. `areas` lists the first path components, capped at 10, with
  `(+)` when truncated. The bracketed rule version tells you how the facts were
  computed. No "observed size" is derived from them.
- `gate: <trigger> (paths: ...)`: shown when the run was gated.
- `unverified: N open (external x, normative y, untested z)`: followed by one
  line per open item, `- U1 [external] text (probe: <outcome> <result>)`.

`carcara status --json` includes the raw `gate`, `unverified`, `probe_results`
and `extent` values.

| Option | Description |
|---|---|
| `-p, --profile NAME` | profile name or `.env` path (default: the profile recorded by `carcara install`, else `balanced`) |
| `--size S\|M\|L` | skip triage and use this size |
| `--yes` / `--approve-plan` | auto-approve the plan gate / gate M plans too |
| `--max-budget-usd USD` | cap the total estimated cost (exit 5 when exceeded); stored with the run and kept on `--resume` unless given again; on a subscription this is a proxy for plan usage |
| `--use-api-key` | let the CLI use `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` (pay-per-token API billing) instead of your subscription login |
| `--dry-run` | show stages, models and tools without calling Claude |
| `--plan-only` | stop after the plan |
| `--resume RUN_ID` / `--list` | continue a stored run / list stored runs |
| `--reject` | with `--resume`: reject the plan awaiting approval |
| `--feedback TEXT` | with `--resume`: re-plan with this feedback, or guide a needs_human retry; `-` reads stdin |
| `--accept-failures` | with `--resume`: finish a needs_human run, accepting its failures |
| `--allow-dirty` | allow uncommitted changes (clean tree required by default); the diff base is a snapshot of your uncommitted work |
| `--unrestricted-bash` | turn off the implementer/test-runner Bash deny-list for this invocation only (prints a warning; pass it again with `--resume`) |
| `--review` | also review S-sized changes |
| `--project-settings` | load the project's Claude Code settings and CLAUDE.md (note: their `env` / `apiKeyHelper` can re-enable API billing) |
| `--cwd DIR` | project directory (default `.`) |

The task may be `-` to read it from stdin. Long options must be spelled out
(abbreviations such as `--ye` are rejected). Only one run is active per
project at a time (exit 6 otherwise), and `carcara run` refuses to start inside
a carcara stage. When run from Claude Code (`CLAUDECODE` set) the approval gate
never waits on stdin: it stops with exit 3 instead. See
[Using carcara from Claude Code](#using-carcara-from-claude-code) for routing.

`carcara status [RUN_ID]` prints a run's report and resume command (`--json`
for machine-readable output, `--plan` for the stored plan); `carcara diff
[RUN_ID] [--stat]` shows the working tree's changes since the run's base,
excluding secrets and `.carcara/`. Both default to the active run, else the
latest one.

The budget is checked from the estimated stage costs the SDK reports. A stage
attempt that errors out but still returns SDK usage counts toward the total and
the `--max-budget-usd` cap, and appears in the report as `<stage> $x.xx
(failed)`. An attempt that dies before the SDK reports a result has unknown
cost: it is flagged as uncounted (`stage_error` with `"uncounted": true` in
`events.jsonl`, `uncounted_stages` in `carcara status --json`, and a note on
the report's total line), and the cap may be exceeded by that unknown amount.

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
| 6 | another run is active (no run was started) |
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

implementer and test-runner may run any Bash command except those caught by a
deny-list: `git push`, `git reset --hard`, `git clean -f`, curl/wget piped or
substituted into a shell or interpreter, shell access to `.env`/`secrets/`
paths, and shell writes (redirections, `tee`, `rm`/`mv`/`cp`/`mkdir`/...)
outside the repo or into `.git`, `.claude` or `.carcara`. Reads outside the
repo stay allowed. Wrappers such as `sudo -u root`, `nice -n 5`, `env -u X`,
`env -S '...'`, `timeout -s KILL 5` and `xargs -n 1` are looked through, including
their option values. To turn the deny-list off, pass
`carcara run --unrestricted-bash`. It applies to that invocation only (a
resume needs it again), prints a warning on stderr and is recorded in the
run's events.

Probes (see [Project config](#project-config-carcaraconfigjson)) are not agent
tools. carcara itself runs them after the review, and they never go through
agent Bash. They are unauthenticated HTTP GETs to URL templates from the
project's allow-list, sent with a 5 s timeout and with no credentials or
cookies. Only the `{arg}` path segment comes from the reviewer, and it is
URL-quoted. Only the status code is read, and the run records just the item
id and a short result. The agent Bash policy above is unchanged.

Known risks:

- implementer and test-runner Bash is guarded only by a best-effort deny-list.
  Variables, `eval`, base64, `cd`, aliases, or a script written into the repo
  and then run all bypass it. It is string matching on the command line, so a
  wrapper option missing from its table can also hide a command. The opt-out
  is a CLI flag resolved before the run starts, so a stage cannot turn it on
  (files a stage creates have no effect). The real safety net is still the clean-tree
  requirement and the base commit recorded for each run. A per-project
  test-command allowlist is not implemented yet.
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

`carcara install` records the chosen profile in `.carcara/profile` (a name, or
the absolute path of a custom `.env` file; `.carcara/` is gitignored, so a fresh
clone has no record). `carcara run` and `carcara run --dry-run` use it unless
`--profile` is given, and fall back to `balanced` when nothing is recorded. If
the recorded profile can't be loaded (e.g. the `.env` file moved), the run
warns on stderr and uses `balanced`. `--resume` always keeps the profile the
run started with. The run's first stderr line shows the profile:
`carcara: run <id> started (profile quality)`.

## Project config (`.carcara/config.json`)

This file is optional, separate from profiles, and read by `carcara run` from
the project directory. Unknown keys are an error. A run snapshots it at start
(`project_config` in `state.json`), and `--resume` uses the snapshot, so editing
the file mid-run has no effect on that run. Agents cannot write it.

```json
{
  "verifiability_paths": [".github/**", "**/migrations/**", "**/auth/**", "**/policy*", "**/policy/**"],
  "probes": {"pypi-name": "https://pypi.org/pypi/{arg}/json"}
}
```

| Key | Meaning |
|---|---|
| `verifiability_paths` | glob patterns (posix paths; `**/` matches any number of directories, `*` matches within one component) that trigger the [approval gate](#carcara-run). A list replaces the defaults shown above; `[]` turns the trigger off. |
| `probes` | named, read-only checks the reviewer may reference from `external` unverified items. Each value is an `http`/`https` URL with exactly one `{arg}` placeholder and no `user:pass@`. Without probes, nothing is fetched. |

A probe result counts as `exists` for a 2xx status and `absent` for 404/410.
It is `confirmed` or `contradicted` against the item's expectation, and any
other status, or an error, counts as `inconclusive`. Probes do not follow
redirects (a 3xx is `inconclusive` with result `redirect`) and ignore proxy
environment variables.

`pyproject.toml` is not gated by default. Only its version/publish sections
matter for releases, and patterns match whole paths. If you want every change
to it gated, add `"pyproject.toml"` to the list.

## Development

```sh
pip install -e '.[dev]'
ruff check .
pytest -q
tests/fixtures/regen_golden.sh     # regenerate golden installer output after template/profile changes
```

See `tests/fixtures/golden/README.md` for the golden and install-snapshot fixtures.

### Release

1. Bump `version` in `pyproject.toml` and commit to `main`.
2. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The `release` workflow (`.github/workflows/release.yml`) checks that the
   tag matches the `pyproject.toml` version, builds and smoke-tests the sdist
   and wheel, and publishes them to PyPI.

One-time setup: on PyPI, add a (pending) trusted publisher for project
`carcara-sdlc` with owner `oshogun`, repository `carcara`, workflow `release.yml`
and environment `pypi`; then create a `pypi` environment in the GitHub repo
settings (optionally with required reviewers). Publishing uses OIDC trusted
publishing, so no API token is needed.
