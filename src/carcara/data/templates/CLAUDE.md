## carcara agentic SDLC (profile: {{PROFILE}})

This project uses the carcara SDLC framework. Work is split across
specialised subagents in `.claude/agents/`, each pinned to the cheapest model
that does its job well:

| Agent | Model | Role |
|---|---|---|
| explorer | {{MODEL_EXPLORER}} | read-only code search, file:line facts |
| architect | {{MODEL_ARCHITECT}} | plans for non-trivial changes |
| implementer | {{MODEL_IMPLEMENTER}} | writes code and tests |
| test-runner | {{MODEL_TEST_RUNNER}} | runs build/lint/tests, summarises |
| reviewer | {{MODEL_REVIEWER}} | high-signal diff review |
| doc-writer | {{MODEL_DOC_WRITER}} | updates affected docs |

Commands: `/sdlc <task>` (full triaged pipeline), `/sdlc-plan`, `/sdlc-build`,
`/sdlc-test`, `/sdlc-review`. `carcara run "<task>"` runs this pipeline
headlessly from the terminal.

### Token discipline
{{ROUTING}}
- Delegate searching to `explorer` and command output to `test-runner`
  instead of reading files or logs in the main conversation.
- Give subagents only the context they need (task, plan, file paths); they
  reply with compact summaries — do not ask them for full files or logs.
- Do not re-read files a subagent already summarised unless you must edit them.
- Reserve extended thinking ("think hard"/"ultrathink") for architect-level
  design questions, not routine edits.
- Cap fix loops at 2 iterations, then ask the user.
- Run `/compact` between large phases of long sessions.
