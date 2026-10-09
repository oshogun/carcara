"""Human gates for ``carcara run``: plan approval and fix-loop continuation."""

from __future__ import annotations

import sys
from typing import Any, TextIO


def render_plan(plan: dict[str, Any]) -> str:
    lines = [f"Gate: {plan['gate_reason']}"] if plan.get("gate_reason") else []
    lines += [f"Plan: {plan.get('goal', '')}", "Steps:"]
    for index, step in enumerate(plan.get("steps", []), 1):
        files = ", ".join(step.get("files", [])) or "-"
        lines.append(f"  {index}. [{step.get('id', index)}] {step.get('change', '')}")
        lines.append(f"     files: {files}")
    for title, key in (("Tests", "tests"), ("Acceptance", "acceptance"), ("Risks", "risks")):
        items = plan.get(key) or []
        lines.append(f"{title}:" + ("" if items else " none"))
        lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines) + "\n"


class TtyGate:
    """Interactive gate; streams default to ``sys.stdin``/``sys.stdout`` at call time.

    ``interactive``: the orchestrator calls it in a worker thread, so the event
    loop (and Urutau reporting) keeps running while it waits for an answer.
    """

    interactive = True

    def __init__(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self._stdin = stdin
        self._stdout = stdout

    @property
    def stdin(self) -> TextIO:
        return self._stdin or sys.stdin

    @property
    def stdout(self) -> TextIO:
        return self._stdout or sys.stdout

    def _ask(self, prompt: str) -> str:
        self.stdout.write(prompt)
        self.stdout.flush()
        return self.stdin.readline().strip().lower()

    def approve_plan(self, plan: dict[str, Any]) -> str:
        self.stdout.write(render_plan(plan))
        answer = self._ask("Approve plan? [y/N/d(efer)] ")
        if answer in ("y", "yes"):
            return "approve"
        if answer in ("d", "defer"):
            return "defer"
        return "reject"

    def ask_continue(self, summary: str) -> bool:
        self.stdout.write(summary + "\n")
        return self._ask("Continue despite unresolved failures? [y/N] ") in ("y", "yes")


class NonInteractiveGate:
    """No TTY and no ``--yes``: defer plan approval, never continue past failures."""

    interactive = False

    def approve_plan(self, plan: dict[str, Any]) -> str:
        return "defer"

    def ask_continue(self, summary: str) -> bool:
        return False
