---
description: Review the current uncommitted diff for bugs and security issues
argument-hint: [optional focus area]
---

Use the `reviewer` subagent to review the current uncommitted changes
(`git diff` and `git diff --staged`). Optional focus: $ARGUMENTS

Relay the verdict and findings. Do not fix anything unless the user asks;
if they do, delegate the fixes to the `implementer` subagent.
