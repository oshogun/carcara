---
description: Run the carcara SDLC pipeline (triage, explore, plan, build, test, review, docs) for a task
argument-hint: <task description>
---

Run the carcara SDLC pipeline for this task:

$ARGUMENTS

You are the orchestrator. Keep this conversation lean: delegate work to the
carcara subagents and pass them only the context they need (task, plan,
file paths). Never paste whole files or full logs into this conversation.

## 1. Triage
Classify the task and state the size in one line before doing anything else:
- **S** — one file or an obvious, local change.
- **M** — a few files, approach is clear.
- **L** — many files, new feature, refactor, or real design trade-offs.

## 2. Pipeline
- **S:** delegate the change to the `implementer` subagent (no
  explorer/architect; main-session edits are blocked while carcara routing is
  on), then use the `test-runner` subagent. Skip review unless the change
  touches security, data handling or public APIs.
- **M:** `explorer` → `implementer` (give it the findings + a 3–6 bullet
  plan you write yourself) → `test-runner` → `reviewer`.
- **L:** `explorer` → `architect` → show the plan to the user and **wait for
  approval** → `implementer` (one plan step or a small group of steps per
  call) → `test-runner` → `reviewer` → `doc-writer` if user-facing behaviour
  changed.

## 3. Fix loop
If the test-runner or reviewer reports problems, send only the failing
items to the `implementer`. Allow at most **2** fix iterations; then stop and
ask the user how to proceed.

## 4. Report
Finish with at most ~10 lines: size, what changed (files), test result,
review verdict, follow-ups.
