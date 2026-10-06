---
description: Explore the codebase and produce an implementation plan (no code changes)
argument-hint: <task description>
---

Produce an implementation plan for this task without changing any files:

$ARGUMENTS

1. Use the `explorer` subagent to gather the relevant files, conventions and
   build/test commands.
2. Pass the task and the explorer's findings to the `architect` subagent.
3. Present the architect's plan to the user verbatim and stop. Do not
   implement anything.
