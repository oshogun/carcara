---
description: Implement an approved plan (or the given instruction) step by step and test it
argument-hint: [plan or instruction; defaults to the plan in this conversation]
---

Implement the following (if empty, use the most recent approved plan in this
conversation):

$ARGUMENTS

1. Hand each plan step (or small group of related steps) to the
   `implementer` subagent with only the file paths and context it needs.
2. After the implementation, use the `test-runner` subagent to run the
   relevant build/lint/test commands.
3. On failures, send only the failing items back to the `implementer`.
   Allow at most 2 fix iterations, then stop and ask the user.
4. Report changed files and the final test result in at most ~10 lines.
