"""The ``carcara run`` state machine.

Pipelines (size from TRIAGE unless given):

- S: IMPLEMENT -> TEST [-> REVIEW if ``review_small``]
- M: EXPLORE -> PLAN (main model, no tools) [-> GATE if ``approve_plan``]
  -> IMPLEMENT -> TEST -> REVIEW
- L: EXPLORE -> ARCHITECT -> GATE -> IMPLEMENT per plan step -> TEST -> REVIEW
  -> DOCS (if any implement output is a user-facing change)

Failing tests or blocker/major review findings trigger at most two fix
iterations (implementer with only the failing items -> test -> review), then
``needs_human``. Every completed stage is stored under a deterministic key, so
``resume`` replays finished stages from the run store without backend calls.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from carcara.backend import Backend, BackendError, build_request
from carcara.policy import is_secret_path
from carcara.profiles import Profile
from carcara.roles import Role, get_role
from carcara.runstore import Run, RunStore

EXIT_CODES = {
    "done": 0,
    "plan_only": 0,
    "failed": 1,
    "awaiting_approval": 3,
    "needs_human": 4,
    "budget_exceeded": 5,
}

# Per-stage max_turns defaults, keyed by pipeline step name.
DEFAULT_MAX_TURNS: dict[str, int] = {
    "triage": 3,
    "explore": 30,
    "plan": 5,
    "architect": 30,
    "implement": 80,
    "test": 30,
    "review": 30,
    "docs": 30,
}

MAX_FIX_ITERATIONS = 2
DIFF_CAP = 20_000


class OrchestratorError(Exception):
    """A run cannot start (not a git repo, dirty tree, unknown run...)."""


class Gate(Protocol):
    def approve_plan(self, plan: dict[str, Any]) -> str:
        """Return ``"approve"``, ``"reject"`` or ``"defer"``."""
        ...

    def ask_continue(self, summary: str) -> bool:
        """Fix loop exhausted: True continues despite failures, False stops."""
        ...


class AutoGate:
    """Non-interactive gate for tests and ``--yes``; records its calls."""

    def __init__(
        self, approve: bool = True, *, decision: str | None = None, continue_: bool = False
    ) -> None:
        self.decision = decision or ("approve" if approve else "reject")
        self.continue_ = continue_
        self.plans: list[dict[str, Any]] = []
        self.summaries: list[str] = []

    def approve_plan(self, plan: dict[str, Any]) -> str:
        self.plans.append(plan)
        return self.decision

    def ask_continue(self, summary: str) -> bool:
        self.summaries.append(summary)
        return self.continue_


@dataclass
class RunOptions:
    size: str | None = None
    review_small: bool = False
    approve_plan: bool = False
    plan_only: bool = False
    max_budget_usd: float | None = None
    allow_dirty: bool = False
    project_settings: bool = False
    use_api_key: bool = False
    max_turns: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MAX_TURNS))


@dataclass
class RunOutcome:
    status: str
    exit_code: int
    report_text: str
    run_id: str | None = None


class _Stop(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise OrchestratorError("git is not installed") from exc


def _dirty_entries(cwd: str) -> list[str]:
    proc = _git(cwd, "status", "--porcelain")
    entries = []
    for line in proc.stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        if path == ".carcara" or path.startswith(".carcara/"):
            continue
        entries.append(line)
    return entries


def _untracked_context(cwd: str, budget: int) -> str:
    """Contents of untracked (non-ignored) files, roughly capped at ``budget``.

    Skips ``.carcara/``, secret paths, symlinks, binary and oversized files.
    """
    proc = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    parts: list[str] = []
    used = 0
    for path in proc.stdout.split("\0"):
        if not path or path == ".carcara" or path.startswith(".carcara/"):
            continue
        if used >= budget:
            parts.append(f"\n### untracked: {path}\n(omitted: diff cap reached)\n")
            continue
        full = os.path.join(cwd, path)
        body: str
        if is_secret_path(path):
            body = "(skipped: secret file)"
        elif os.path.islink(full) or not os.path.isfile(full):
            body = "(skipped: not a regular file)"
        else:
            try:
                if os.path.getsize(full) > budget - used:
                    body = "(skipped: too large)"
                else:
                    with open(full, "rb") as fh:
                        data = fh.read()
                    if b"\0" in data:
                        body = "(skipped: binary)"
                    else:
                        body = data.decode("utf-8", errors="replace")
            except OSError:
                body = "(skipped: unreadable)"
        part = f"\n### untracked: {path}\n{body}\n"
        parts.append(part)
        used += len(part)
    return "".join(parts)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _failing_items(test: dict[str, Any], review: dict[str, Any] | None) -> dict[str, Any]:
    if not test.get("passed"):
        return {"test_failures": test.get("failures", []), "commands": test.get("commands", [])}
    if review is not None:
        serious = [f for f in review["findings"] if f["severity"] in ("blocker", "major")]
        if serious:
            return {"review_findings": serious}
    return {}


class Orchestrator:
    def __init__(
        self,
        backend: Backend,
        profile: Profile,
        cwd: str,
        store: RunStore,
        gate: Gate,
        options: RunOptions | None = None,
    ) -> None:
        self.backend = backend
        self.profile = profile
        self.cwd = str(cwd)
        self.store = store
        self.gate = gate
        self.options = options or RunOptions()
        self.run_state: Run | None = None
        self._roles: dict[str, Role] = {}

    # -- public entry points -------------------------------------------------

    async def run(self, task: str) -> RunOutcome:
        proc = _git(self.cwd, "rev-parse", "--verify", "HEAD")
        if proc.returncode != 0:
            raise OrchestratorError(
                f"{self.cwd} is not a git repository with at least one commit; "
                "carcara run needs a base commit to diff against"
            )
        base_sha = proc.stdout.strip()
        if not self.options.allow_dirty:
            dirty = _dirty_entries(self.cwd)
            if dirty:
                raise OrchestratorError(
                    "working tree has uncommitted changes; commit or stash them, "
                    "or pass --allow-dirty:\n" + "\n".join(dirty[:10])
                )
        run = self.store.create(task, self.profile.name, base_sha, size=self.options.size)
        # Absolute path so ``carcara run --resume`` can reload custom profiles.
        run.state["profile_source"] = os.path.abspath(self.profile.source)
        run.state["use_api_key"] = self.options.use_api_key
        run.save()
        return await self._drive(run)

    async def resume(self, run_id: str) -> RunOutcome:
        try:
            run = self.store.load(run_id)
        except Exception as exc:
            raise OrchestratorError(str(exc)) from exc
        status = run.state["status"]
        if status == "done":
            report = run.read_report() or self._report(run)
            return RunOutcome("done", 0, report, run.id)
        run.event("resumed", previous_status=status)
        if self.options.use_api_key and not run.state.get("use_api_key"):
            run.state["use_api_key"] = True
            run.save()
        if status == "needs_human":
            # The human fixed things: verify again under fresh stage keys.
            run.state["verify_round"] = int(run.state.get("verify_round", 0)) + 1
            run.save()
        return await self._drive(run)

    # -- driver --------------------------------------------------------------

    async def _drive(self, run: Run) -> RunOutcome:
        self.run_state = run
        run.set_status("running")
        try:
            status, message = await self._pipeline(run)
        except _Stop as stop:
            status, message = stop.status, stop.message
        run.set_status(status, message)
        report = self._report(run)
        run.write_report(report)
        return RunOutcome(status, EXIT_CODES[status], report, run.id)

    def _role(self, name: str) -> Role:
        if name not in self._roles:
            self._roles[name] = get_role(name)
        return self._roles[name]

    async def _stage(
        self,
        key: str,
        stage: str,
        role_name: str | None,
        turns_key: str,
        prompt: Callable[[], str],
    ) -> dict[str, Any]:
        run = self.run_state
        assert run is not None
        done = run.stage(key)
        if done is not None:
            run.event("stage_replayed", key=key)
            return done["output"]

        remaining: float | None = None
        if self.options.max_budget_usd is not None:
            remaining = round(self.options.max_budget_usd - run.state["totals"]["cost_usd"], 6)
            if remaining <= 0:
                raise _Stop("budget_exceeded", f"budget exhausted before stage {key}")

        role = self._role(role_name) if role_name else None
        request = build_request(
            stage,
            role,
            self.profile,
            prompt(),
            self.cwd,
            max_turns=self.options.max_turns.get(turns_key),
            max_budget_usd=remaining,
            setting_sources=["project"] if self.options.project_settings else [],
        )
        run.event("stage_started", key=key, stage=stage, role=role_name, model=request.model)
        try:
            result = await self.backend.run_stage(request)
        except BackendError as exc:
            # Count whatever the stage spent (e.g. a result with missing or
            # invalid structured output); with no StageResult the cost is unknown.
            if exc.result is not None:
                failed = exc.result
                run.add_cost(failed.cost_usd, failed.usage, failed.num_turns)
                run.save()
            run.event("stage_error", key=key, error=str(exc))
            raise _Stop("failed", f"stage {key} failed: {exc}") from exc

        run.add_cost(result.cost_usd, result.usage, result.num_turns)
        if result.subtype == "error_max_budget_usd":
            run.save()
            run.event("stage_error", key=key, subtype=result.subtype, cost_usd=result.cost_usd)
            raise _Stop("budget_exceeded", f"budget exhausted during stage {key}")
        if result.is_error:
            run.save()
            detail = (
                "; ".join(result.errors)
                or (result.text or "").strip()[:500]
                or f"error result (subtype {result.subtype})"
            )
            run.event("stage_error", key=key, subtype=result.subtype, errors=result.errors)
            raise _Stop("failed", f"stage {key} failed: {detail}")

        output = result.structured
        if stage == "implement" and output.get("blocked"):
            # Not memoised: a resume re-runs the stage after the human intervenes.
            run.save()
            run.event("stage_blocked", key=key, output=output)
            raise _Stop("needs_human", f"implementer blocked at {key}: {output.get('notes', '')}")
        run.record_stage(
            key,
            stage,
            role_name,
            request.model,
            output,
            result.cost_usd,
            result.usage,
            result.num_turns,
            result.session_id,
        )
        run.event("stage_completed", key=key, cost_usd=result.cost_usd)
        return output

    # -- git context ---------------------------------------------------------

    def _diff_context(self) -> str:
        assert self.run_state is not None
        base = self.run_state.state["base_sha"]
        stat = _git(self.cwd, "diff", "--stat", base).stdout.strip()
        diff = _git(self.cwd, "diff", base).stdout
        if len(diff) < DIFF_CAP:
            diff += _untracked_context(self.cwd, DIFF_CAP - len(diff))
        if len(diff) > DIFF_CAP:
            diff = diff[:DIFF_CAP] + f"\n[... diff truncated at {DIFF_CAP} chars ...]"
        status = "\n".join(_dirty_entries(self.cwd))
        return (
            f"## Changes since base {base}\n"
            f"### git status --porcelain\n{status or '(clean)'}\n"
            f"### git diff --stat\n{stat or '(no diff)'}\n"
            f"### git diff\n{diff or '(no diff)'}\n"
        )

    # -- pipeline ------------------------------------------------------------

    async def _pipeline(self, run: Run) -> tuple[str, str | None]:
        task = run.state["task"]
        size = run.state.get("size") or self.options.size
        if not size:
            triage = await self._stage(
                "triage",
                "triage",
                None,
                "triage",
                lambda: (
                    f"Task: {task}\n\nClassify the task size for the SDLC pipeline: "
                    "S = small/local change, M = multi-file change needing a short plan, "
                    "L = large/cross-cutting change needing an architect plan."
                ),
            )
            size = triage["size"]
        if run.state.get("size") != size:
            run.state["size"] = size
            run.save()

        implements: list[dict[str, Any]] = []
        review_on = True
        if size == "S":
            review_on = self.options.review_small
            if self.options.plan_only:
                await self._plan_stage(task, None, architect=False)
                return "plan_only", None
            implements.append(
                await self._stage(
                    "implement",
                    "implement",
                    "implementer",
                    "implement",
                    lambda: f"Task: {task}\n\nImplement this small change and add tests.",
                )
            )
        else:
            explore = await self._stage(
                "explore",
                "explore",
                "explorer",
                "explore",
                lambda: (
                    f"Task: {task}\n\nFind the files, symbols and conventions relevant to "
                    "this task. Report file:line facts only."
                ),
            )
            plan = await self._plan_stage(task, explore, architect=size == "L")
            if self.options.plan_only:
                return "plan_only", None
            if size == "L" or self.options.approve_plan:
                self._gate(run, plan)
            steps = plan["steps"] if size == "L" else []
            if steps:
                seen: set[str] = set()
                for index, step in enumerate(steps, 1):
                    step_id = str(step["id"]) or str(index)
                    if step_id in seen:
                        step_id = f"{step_id}-{index}"
                    seen.add(step_id)
                    implements.append(
                        await self._stage(
                            f"implement:{step_id}",
                            "implement",
                            "implementer",
                            "implement",
                            self._step_prompt(task, plan, step),
                        )
                    )
            else:
                implements.append(
                    await self._stage(
                        "implement",
                        "implement",
                        "implementer",
                        "implement",
                        lambda: (
                            f"Task: {task}\n\nApproved plan (JSON):\n{_dumps(plan)}\n\n"
                            "Implement the plan, including its tests."
                        ),
                    )
                )

        message, fixes = await self._verify(run, task, review_on)
        implements.extend(fixes)

        # All implement outputs, including fixes from earlier verify rounds.
        implements = [e["output"] for e in run.state["stages"] if e["stage"] == "implement"]
        if size == "L" and any(out.get("user_facing_change") for out in implements):
            changed = [c for out in implements for c in out["changed"]]
            await self._stage(
                "docs",
                "docs",
                "doc-writer",
                "docs",
                lambda: (
                    f"Task: {task}\n\nChanged files (JSON):\n{_dumps(changed)}\n\n"
                    "Update the documentation affected by these user-facing changes."
                ),
            )
        return "done", message

    async def _plan_stage(
        self, task: str, explore: dict[str, Any] | None, *, architect: bool
    ) -> dict[str, Any]:
        context = f"\n\nExplorer findings (JSON):\n{_dumps(explore)}" if explore else ""
        prompt = (
            f"Task: {task}{context}\n\nWrite an implementation plan: ordered steps with "
            "stable ids and files, tests, acceptance criteria and risks."
        )
        if architect:
            return await self._stage("architect", "plan", "architect", "architect", lambda: prompt)
        return await self._stage("plan", "plan", None, "plan", lambda: prompt)

    def _step_prompt(
        self, task: str, plan: dict[str, Any], step: dict[str, Any]
    ) -> Callable[[], str]:
        def build() -> str:
            return (
                f"Task: {task}\n\nApproved plan (JSON):\n{_dumps(plan)}\n\n"
                f"Implement ONLY step {step['id']} now (JSON):\n{_dumps(step)}\n"
                "Earlier steps are already applied in the working tree."
            )

        return build

    def _gate(self, run: Run, plan: dict[str, Any]) -> None:
        if run.state.get("plan_approved"):
            return
        decision = self.gate.approve_plan(plan)
        run.event("gate", decision=decision)
        if decision == "approve":
            run.state["plan_approved"] = True
            run.save()
            return
        if decision == "defer":
            raise _Stop(
                "awaiting_approval",
                f"plan awaits approval; resume with: carcara run --resume {run.id} --yes",
            )
        raise _Stop("failed", "plan rejected")

    async def _verify(
        self, run: Run, task: str, review_on: bool
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """TEST -> REVIEW with the capped fix loop. Returns (note, fix outputs)."""
        rnd = int(run.state.get("verify_round", 0))
        prefix = f"retry-{rnd}:" if rnd else ""
        fixes: list[dict[str, Any]] = []

        def test_prompt() -> str:
            return (
                f"Task: {task}\n\nRun the project's build, lint and tests relevant to the "
                "change and report the results."
            )

        def review_prompt() -> str:
            return (
                f"Task: {task}\n\nReview this change for correctness, security and "
                f"missing tests. Report only real issues.\n\n{self._diff_context()}"
            )

        async def check(stage_prefix: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
            test = await self._stage(
                f"{stage_prefix}test", "test", "test-runner", "test", test_prompt
            )
            review = None
            if review_on and test["passed"]:
                review = await self._stage(
                    f"{stage_prefix}review", "review", "reviewer", "review", review_prompt
                )
            return test, review

        test, review = await check(prefix)
        failing = _failing_items(test, review)
        for i in range(1, MAX_FIX_ITERATIONS + 1):
            if not failing:
                break
            items = failing

            def fix_prompt(items: dict[str, Any] = items) -> str:
                return (
                    f"Task: {task}\n\nFix ONLY these failing items (JSON):\n{_dumps(items)}\n\n"
                    f"{self._diff_context()}"
                )

            fix_prefix = f"{prefix}fix-{i}:"
            fixes.append(
                await self._stage(
                    f"{fix_prefix}implement", "implement", "implementer", "implement", fix_prompt
                )
            )
            test, review = await check(fix_prefix)
            failing = _failing_items(test, review)

        if not failing:
            return None, fixes
        summary = f"still failing after {MAX_FIX_ITERATIONS} fix iterations: {_dumps(failing)}"
        run.event("fix_loop_exhausted", failing=failing)
        if self.gate.ask_continue(summary):
            run.state["accepted_failures"] = failing
            run.save()
            return "continued despite unresolved failures (user accepted)", fixes
        raise _Stop("needs_human", summary)

    # -- report --------------------------------------------------------------

    def _report(self, run: Run) -> str:
        state = run.state
        stages = state["stages"]
        files: list[str] = []
        test_line = "not run"
        review_line = "not run"
        for entry in stages:
            out = entry["output"]
            if entry["stage"] == "implement":
                for change in out["changed"]:
                    if change["path"] not in files:
                        files.append(change["path"])
            elif entry["stage"] == "test":
                if out["passed"]:
                    test_line = f"passed ({len(out['commands'])} commands)"
                else:
                    names = ", ".join(f["name"] for f in out["failures"][:5])
                    test_line = f"FAILED: {names or 'see test output'}"
            elif entry["stage"] == "review":
                serious = sum(1 for f in out["findings"] if f["severity"] in ("blocker", "major"))
                review_line = (
                    f"{out['verdict']} ({len(out['findings'])} findings, {serious} serious)"
                )
        costs = ", ".join(f"{e['key']} ${e['cost_usd']:.2f}" for e in stages) or "none"
        try:
            rel_dir = run.dir.relative_to(self.store.cwd)
        except ValueError:
            rel_dir = run.dir
        lines = [
            f"carcara run {run.id}: {state['status']} (size {state.get('size') or '?'})",
            f"files changed: {', '.join(files) if files else 'none'}",
            f"tests: {test_line}",
            f"review: {review_line}",
            f"est. cost: {costs}",
            f"total est. cost: ${state['totals']['cost_usd']:.2f} "
            f"({'API key' if state.get('use_api_key') else 'subscription login'})",
            f"run dir: {rel_dir}",
        ]
        if state.get("message"):
            note = state["message"]
            lines.append(f"note: {note if len(note) <= 300 else note[:297] + '...'}")
        return "\n".join(lines) + "\n"


__all__ = [
    "AutoGate",
    "DEFAULT_MAX_TURNS",
    "EXIT_CODES",
    "Gate",
    "Orchestrator",
    "OrchestratorError",
    "RunOptions",
    "RunOutcome",
]
