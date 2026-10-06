<!-- carcara:begin -->
## carcara agentic SDLC (profile: quality)

This project uses the carcara SDLC framework. Work is split across
specialised subagents in `.claude/agents/`, each pinned to the cheapest model
that does its job well:

| Agent | Model | Role |
|---|---|---|
| explorer | sonnet | read-only code search, file:line facts |
| architect | opus | plans for non-trivial changes |
| implementer | opus | writes code and tests |
| test-runner | haiku | runs build/lint/tests, summarises |
| reviewer | opus | high-signal diff review |
| doc-writer | sonnet | updates affected docs |

Commands: `/sdlc <task>` (full triaged pipeline), `/sdlc-plan`, `/sdlc-build`,
`/sdlc-test`, `/sdlc-review`. `carcara run "<task>"` runs this pipeline
headlessly from the terminal.

### Token discipline
- Code changes in this repo are routed to the carcara orchestrator: use the
  `carcara` skill (it runs `carcara run`); don't edit project files directly.
  `/sdlc*` remain available as a manual path.
- Delegate searching to `explorer` and command output to `test-runner`
  instead of reading files or logs in the main conversation.
- Give subagents only the context they need (task, plan, file paths); they
  reply with compact summaries — do not ask them for full files or logs.
- Do not re-read files a subagent already summarised unless you must edit them.
- Reserve extended thinking ("think hard"/"ultrathink") for architect-level
  design questions, not routine edits.
- Cap fix loops at 2 iterations, then ask the user.
- Run `/compact` between large phases of long sessions.
<!-- carcara:end -->
