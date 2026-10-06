---
name: carcara
description: Route code changes in this repository through the carcara orchestrator (`carcara run`), which explores, plans, implements, tests and reviews them with specialised subagents.
when_to_use: Use for ANY request to change code or files in this repository - implement a feature, fix a bug, refactor, add or update tests, update config or docs in the codebase. Do not use for questions, explanations, or code reviews that change nothing, or when the user explicitly invokes /sdlc or another /sdlc-* command.
---
<!-- carcara:skill - installed by `carcara install`; `carcara install --no-routing` removes this file -->

# carcara driver protocol

You drive carcara; carcara does the work. Be concise and keep the user informed
in 1-3 lines per step. Never paste full logs or whole reports.

## 1. Never edit project files yourself
For a change request, do not use Edit/Write/MultiEdit/NotebookEdit on project
files (a hook blocks it). Clarify the request if needed, then hand it to carcara.

## 2. Check for existing runs
Run `carcara status --json`.
- Exit 1 with `no runs` on stderr: nothing pending, go to step 3.
- `"active": true`: a run is in progress. Tell the user and offer to check
  `carcara status` again later; do not start another run.
- `status` is `awaiting_approval`, `needs_human` or `budget_exceeded`: a paused
  run exists. Ask the user (AskUserQuestion) whether to resume it (go to step 4
  with that run) or start the new request. Abandoning the paused run is fine; it
  stays on disk.
- Anything else (`done`, `failed`, ...): go to step 3.
Older paused runs are listed in the carcara prompt context and by
`carcara run --list`; treat them the same way.

## 3. Start the run
Use Bash with `run_in_background: true`, passing the task through a quoted
heredoc (never in shell arguments):

    carcara run --allow-dirty - <<'CARCARA_TASK'
    <the user's request verbatim, plus any clarified details>
    CARCARA_TASK

Never add `--yes`, `--accept-failures` or `--use-api-key` yourself. The first
stderr line is `carcara: run <id> started`; remember the id. Tell the user the
run started; keep chatting if they want. You are notified when it finishes.

## 4. On completion
Take the exit code of the background `carcara run`, read
`carcara status <id> --json` (its `exit_code` / `status` agree), and branch:
- **0 done** (or plan_only): summarise the report - files changed, tests,
  review verdict, estimated cost - and mention `carcara diff <id>`.
- **3 awaiting_approval**: show the output of `carcara status <id> --plan`, then
  ask the user (AskUserQuestion): approve / request changes / reject.
  - Approve: background `carcara run --resume <id> --yes`. The user gets a
    permission prompt; that prompt is their approval.
  - Request changes: background
    `carcara run --resume <id> --feedback - <<'CARCARA_FEEDBACK'` with their
    feedback, then `CARCARA_FEEDBACK`. It re-plans and comes back as 3.
  - Reject: `carcara run --resume <id> --reject`.
- **4 needs_human**: summarise `failing`, then ask: give guidance (background
  `carcara run --resume <id> --feedback - <<'CARCARA_FEEDBACK'` heredoc), accept
  the failures (`carcara run --resume <id> --accept-failures`; the user confirms
  in the permission prompt), or stop.
- **5 budget_exceeded**: tell the user the budget was hit and report the spend,
  then ask whether to resume (background `carcara run --resume <id>`, optionally
  with a higher `--max-budget-usd N`). The run keeps its original cap unless a
  new one is given; resuming prompts the user for confirmation.
- **6 busy** (no run was started): another run is active; show `carcara status`.
- **1 failed / other**: show `message`; if `resume_cmd` is set, offer to run it.

## 5. Keep it short
Report progress and results in 1-3 lines; point to `carcara status <id>` and
`carcara diff <id>` for details.
