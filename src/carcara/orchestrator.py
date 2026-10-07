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
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from carcara.backend import (
    Backend,
    BackendError,
    NoStructuredOutput,
    StageResult,
    build_request,
)
from carcara.profiles import Profile
from carcara.roles import Role, get_role
from carcara.runstore import Run, RunBusy, RunStore

EXIT_CODES = {
    "done": 0,
    "plan_only": 0,
    "failed": 1,
    "awaiting_approval": 3,
    "needs_human": 4,
    "budget_exceeded": 5,
    "busy": 6,  # another run holds .carcara/active.json (not a run status)
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

# Appended to a stage's prompt when retrying after it returned no structured output.
_STRUCTURED_OUTPUT_NUDGE = (
    "\n\nYour previous attempt at this stage ended without a result. When done, you"
    " MUST call the StructuredOutput tool exactly once with a result matching the"
    " stage's output schema."
)


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


# Repo config must not run code during carcara's own git calls.
_GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
_DIFF_SAFE = ("--no-ext-diff", "--no-textconv", "--no-color")
# Paths never captured in snapshots nor shown in diffs.
_SECRET_PATHSPECS = (
    ":(icase,glob)**/.env",
    ":(icase,glob)**/.env.*",
    ":(icase,glob)**/secrets/**",
)
_SNAPSHOT_PATHSPECS = (
    ".",
    ":(exclude).carcara",
    *(":(exclude," + spec[2:] for spec in _SECRET_PATHSPECS),
)


def _git(
    cwd: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **env} if env else None
    try:
        return subprocess.run(
            ["git", *_GIT_SAFE, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=full_env,
        )
    except FileNotFoundError as exc:
        raise OrchestratorError("git is not installed") from exc


def _git_ok(cwd: str, *args: str, env: dict[str, str] | None = None) -> str:
    proc = _git(cwd, *args, env=env)
    if proc.returncode != 0:
        raise OrchestratorError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _dirty_entries(cwd: str) -> list[str]:
    proc = _git(cwd, "status", "--porcelain")
    entries = []
    for line in proc.stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        if path == ".carcara" or path.startswith(".carcara/"):
            continue
        entries.append(line)
    return entries


def _snapshot_tree(cwd: str) -> str:
    """Tree sha of the working tree (minus .carcara/ and secrets), via a temp index.

    The temp index is seeded from the real one (stat cache, tracked files), so
    the user's index is never touched.
    """
    real_index = _git_ok(cwd, "rev-parse", "--git-path", "index")
    real_index = os.path.join(cwd, real_index)
    tmp_dir = tempfile.mkdtemp(prefix="carcara-index-")
    try:
        tmp_index = os.path.join(tmp_dir, "index")
        if os.path.isfile(real_index):
            # copy2 keeps the index mtime, which git's racy-git check compares
            # entry mtimes against; a fresh mtime would trust stale stat data.
            shutil.copy2(real_index, tmp_index)
        env = {"GIT_INDEX_FILE": tmp_index}
        if _git(cwd, "add", "-A", "--", *_SNAPSHOT_PATHSPECS, env=env).returncode != 0:
            # Unusable seed (e.g. split index): start over from an empty index.
            if not os.path.exists(tmp_index):
                raise OrchestratorError("git add failed while snapshotting the working tree")
            os.remove(tmp_index)
            # Seed from HEAD so tracked secrets keep their committed content.
            if _git(cwd, "rev-parse", "--verify", "-q", "HEAD", env=env).returncode == 0:
                _git_ok(cwd, "read-tree", "HEAD", env=env)
            _git_ok(cwd, "add", "-A", "--", *_SNAPSHOT_PATHSPECS, env=env)
        # Tracked secrets keep their indexed content (never the working copy):
        # dropping them would make a plain ``git diff <base>`` show them as new
        # files. Diffs exclude them via the secret pathspecs instead.
        return _git_ok(cwd, "write-tree", env=env)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def require_toplevel(cwd: str) -> None:
    """Refuse a ``cwd`` that is not the repository root (pathspecs and the
    run store are relative to it)."""
    proc = _git(cwd, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise OrchestratorError(f"{cwd} is not a git repository")
    top = proc.stdout.strip()
    if os.path.realpath(top) != os.path.realpath(cwd):
        raise OrchestratorError(f"run from the repository root ({top})")


def _safe_diff(cwd: str, base: str, tree: str, *opts: str, check: bool = True) -> str:
    """``git diff`` of ``base`` vs ``tree`` without secrets, .carcara/ or repo diff drivers."""
    proc = _git(cwd, "diff", *_DIFF_SAFE, *opts, base, tree, "--", *_SNAPSHOT_PATHSPECS)
    if check and proc.returncode != 0:
        raise OrchestratorError(f"git diff failed: {proc.stderr.strip()}")
    return proc.stdout


def run_diff(cwd: str, base: str, *, stat: bool = False) -> str:
    """Changes in the working tree since ``base`` (what ``carcara diff`` prints)."""
    tree = _snapshot_tree(cwd)
    return _safe_diff(cwd, base, tree, *(("--stat",) if stat else ()))


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


def _last_failing(run: Run) -> dict[str, Any]:
    """Failing items of the latest test stage (and the review that followed it)."""
    stages = run.state["stages"]
    last = max((i for i, e in enumerate(stages) if e["stage"] == "test"), default=None)
    if last is None:
        return {}
    review = next((e["output"] for e in stages[last + 1 :] if e["stage"] == "review"), None)
    return _failing_items(stages[last]["output"], review)


def _check_resume_flags(
    status: str, reject: bool, feedback: str | None, accept_failures: bool
) -> None:
    """Validate --reject/--feedback/--accept-failures against the run status."""
    if reject and status != "awaiting_approval":
        raise OrchestratorError(f"--reject needs a run awaiting approval (run is {status})")
    if feedback and status not in ("awaiting_approval", "needs_human"):
        raise OrchestratorError(
            f"--feedback needs a run awaiting approval or needing a human (run is {status})"
        )
    if accept_failures and status != "needs_human":
        raise OrchestratorError(f"--accept-failures needs a needs_human run (run is {status})")
    if accept_failures and (reject or feedback):
        raise OrchestratorError("--accept-failures cannot be combined with --reject/--feedback")


class Orchestrator:
    def __init__(
        self,
        backend: Backend,
        profile: Profile,
        cwd: str,
        store: RunStore,
        gate: Gate,
        options: RunOptions | None = None,
        on_start: Callable[[str, bool], None] | None = None,
    ) -> None:
        self.backend = backend
        self.profile = profile
        self.cwd = str(cwd)
        self.store = store
        self.gate = gate
        self.options = options or RunOptions()
        self.run_state: Run | None = None
        # Called as on_start(run_id, resumed) once the run holds the lock.
        self.on_start = on_start
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
        require_toplevel(self.cwd)
        if not self.options.allow_dirty:
            dirty = _dirty_entries(self.cwd)
            if dirty:
                raise OrchestratorError(
                    "working tree has uncommitted changes; commit or stash them, "
                    "or pass --allow-dirty:\n" + "\n".join(dirty[:10])
                )
        holder = self.store.active()
        if holder is not None:
            raise RunBusy(holder["run_id"])
        run = self.store.create(task, self.profile.name, base_sha, size=self.options.size)
        try:
            self.store.acquire_lock(run.id)
        except RunBusy as exc:
            # Lost a race with another starter after the pre-check.
            run.set_status("failed", str(exc))
            raise
        try:
            self._started(run, resumed=False)
            # Absolute path so ``carcara run --resume`` can reload custom profiles.
            run.state["profile_source"] = os.path.abspath(self.profile.source)
            run.state["use_api_key"] = self.options.use_api_key
            # Persisted so a resume keeps the cap unless --max-budget-usd is given again.
            run.state["max_budget_usd"] = self.options.max_budget_usd
            try:
                self._pin_base(run, snapshot=self.options.allow_dirty)
            except OrchestratorError as exc:
                run.set_status("failed", str(exc))
                raise
            return await self._drive(run)
        finally:
            self.store.release_lock(run.id)

    async def resume(
        self,
        run_id: str,
        *,
        reject: bool = False,
        feedback: str | None = None,
        accept_failures: bool = False,
    ) -> RunOutcome:
        require_toplevel(self.cwd)
        run = self._load(run_id)
        status = run.state["status"]
        _check_resume_flags(status, reject, feedback, accept_failures)
        if status == "done":
            report = run.read_report() or self._report(run)
            return RunOutcome("done", 0, report, run.id)
        self.store.acquire_lock(run.id)
        try:
            # Another process may have driven the run before we got the lock.
            run = self._load(run_id)
            status = run.state["status"]
            _check_resume_flags(status, reject, feedback, accept_failures)
            if status == "done":
                report = run.read_report() or self._report(run)
                return RunOutcome("done", 0, report, run.id)
            self._started(run, resumed=True)
            run.event("resumed", previous_status=status)
            if self.options.use_api_key and not run.state.get("use_api_key"):
                run.state["use_api_key"] = True
                run.save()
            if self.options.max_budget_usd is None:
                stored = run.state.get("max_budget_usd")
                self.options = replace(
                    self.options, max_budget_usd=None if stored is None else float(stored)
                )
            else:
                run.state["max_budget_usd"] = self.options.max_budget_usd
                run.save()
            if reject and not feedback:
                run.event("plan_rejected")
                return self._finish(run, "failed", "plan rejected by user")
            if accept_failures:
                run.state["accepted_failures"] = True
                run.save()
                run.event("failures_accepted")
                return self._finish(run, "done", "unresolved failures accepted by user")
            if status == "awaiting_approval" and not any(
                e["stage"] == "implement" for e in run.state["stages"]
            ):
                # Edits made while reviewing the plan are the user's, not carcara's.
                self._pin_base(run, snapshot=True)
            if status == "awaiting_approval" and feedback:
                # Re-plan: a new revision under fresh plan stage keys.
                run.state["plan_revision"] = int(run.state.get("plan_revision", 0)) + 1
                run.state.setdefault("plan_feedback", []).append(feedback)
                run.state["plan_approved"] = False
                run.save()
                run.event("plan_revision", revision=run.state["plan_revision"])
            if status == "needs_human":
                # The human fixed things: verify again under fresh stage keys.
                rnd = int(run.state.get("verify_round", 0)) + 1
                run.state["verify_round"] = rnd
                if feedback:
                    run.state.setdefault("retry_feedback", {})[str(rnd)] = feedback
                blocked = run.state.pop("blocked", None)
                run.state.pop("blocked_feedback", None)
                if blocked and feedback:
                    # Applied to the re-run of the blocked implement stage.
                    run.state["blocked_feedback"] = {
                        **blocked,
                        "feedback": feedback,
                        "round": rnd,
                        "applied": False,
                    }
                run.save()
            return await self._drive(run)
        finally:
            self.store.release_lock(run.id)

    def _load(self, run_id: str) -> Run:
        try:
            return self.store.load(run_id)
        except Exception as exc:
            raise OrchestratorError(str(exc)) from exc

    def _pin_base(self, run: Run, *, snapshot: bool) -> None:
        """Set the run's diff base and pin it at refs/carcara/<run_id>.

        With ``snapshot`` and a dirty tree, the base is a commit of the current
        working tree (secrets and .carcara/ excluded) on top of HEAD, so the
        user's uncommitted work is not attributed to carcara.
        """
        head = _git_ok(self.cwd, "rev-parse", "--verify", "HEAD^{commit}")
        base, kind = head, "head"
        if snapshot and _dirty_entries(self.cwd):
            tree = _snapshot_tree(self.cwd)
            base = _git_ok(
                self.cwd,
                "-c",
                "user.name=carcara",
                "-c",
                "user.email=carcara@localhost",
                "commit-tree",
                "--no-gpg-sign",
                tree,
                "-p",
                head,
                "-m",
                f"carcara base {run.id}",
            )
            kind = "snapshot"
        _git_ok(self.cwd, "update-ref", f"refs/carcara/{run.id}", base)
        run.state["base_sha"] = base
        run.state["base_kind"] = kind
        run.save()
        run.event("base_pinned", base_sha=base, base_kind=kind)

    def _started(self, run: Run, *, resumed: bool) -> None:
        self.run_state = run
        if self.on_start is not None:
            self.on_start(run.id, resumed)

    # -- driver --------------------------------------------------------------

    def _finish(self, run: Run, status: str, message: str | None) -> RunOutcome:
        run.set_status(status, message)
        report = self._report(run)
        run.write_report(report)
        return RunOutcome(status, EXIT_CODES[status], report, run.id)

    async def _drive(self, run: Run) -> RunOutcome:
        self.run_state = run
        run.set_status("running")
        try:
            status, message = await self._pipeline(run)
        except _Stop as stop:
            status, message = stop.status, stop.message
        return self._finish(run, status, message)

    def _role(self, name: str) -> Role:
        if name not in self._roles:
            self._roles[name] = get_role(name)
        return self._roles[name]

    @staticmethod
    def _record_failure(
        run: Run,
        key: str,
        stage: str,
        role_name: str | None,
        model: str,
        result: StageResult | None,
        error: str,
    ) -> dict[str, Any]:
        """Record an errored attempt (its cost is already in the totals, if known).

        Returns the cost fields for the ``stage_error`` event.
        """
        if result is None:
            run.record_failed_attempt(key, stage, role_name, model, error, None, None, 0, False)
            return {"cost_usd": None, "uncounted": True}
        run.record_failed_attempt(
            key,
            stage,
            role_name,
            model,
            error,
            result.cost_usd,
            result.usage,
            result.num_turns,
            True,
        )
        return {"cost_usd": result.cost_usd, "usage": result.usage, "num_turns": result.num_turns}

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

        role = self._role(role_name) if role_name else None
        text = prompt()
        blocked = run.state.get("blocked_feedback")
        apply_guidance = (
            stage == "implement"
            and blocked is not None
            and blocked.get("key") == key
            and not blocked.get("applied")
        )
        if apply_guidance:
            text += (
                f"\n\nA previous attempt at this stage was blocked: {blocked.get('notes', '')}"
                f"\n\nUser guidance:\n{blocked['feedback']}"
            )
        for attempt in range(2):
            remaining: float | None = None
            if self.options.max_budget_usd is not None:
                remaining = round(self.options.max_budget_usd - run.state["totals"]["cost_usd"], 6)
                if remaining <= 0:
                    raise _Stop("budget_exceeded", f"budget exhausted before stage {key}")
            request = build_request(
                stage,
                role,
                self.profile,
                text if attempt == 0 else text + _STRUCTURED_OUTPUT_NUDGE,
                self.cwd,
                max_turns=self.options.max_turns.get(turns_key),
                max_budget_usd=remaining,
                setting_sources=["project"] if self.options.project_settings else [],
            )
            run.event("stage_started", key=key, stage=stage, role=role_name, model=request.model)
            try:
                result = await self.backend.run_stage(request)
                break
            except BackendError as exc:
                # Count whatever the stage spent (e.g. a result with missing or
                # invalid structured output); with no StageResult the cost is unknown.
                if exc.result is not None:
                    failed = exc.result
                    run.add_cost(failed.cost_usd, failed.usage, failed.num_turns)
                cost = self._record_failure(
                    run, key, stage, role_name, request.model, exc.result, str(exc)
                )
                run.event("stage_error", key=key, error=str(exc), **cost)
                if isinstance(exc, NoStructuredOutput) and attempt == 0:
                    # A missing result is usually a one-off slip: retry once.
                    run.event("stage_retry", key=key, reason=str(exc))
                    continue
                raise _Stop("failed", f"stage {key} failed: {exc}") from exc
            except BaseException as exc:
                # Unexpected error or interrupt: the stage's spend is unknown.
                cost = self._record_failure(
                    run, key, stage, role_name, request.model, None, repr(exc)
                )
                run.event("stage_error", key=key, error=repr(exc), **cost)
                raise

        run.add_cost(result.cost_usd, result.usage, result.num_turns)
        if result.subtype == "error_max_budget_usd":
            cost = self._record_failure(
                run, key, stage, role_name, request.model, result, "budget exhausted"
            )
            run.event("stage_error", key=key, subtype=result.subtype, **cost)
            raise _Stop("budget_exceeded", f"budget exhausted during stage {key}")
        if result.is_error:
            detail = (
                "; ".join(result.errors)
                or (result.text or "").strip()[:500]
                or f"error result (subtype {result.subtype})"
            )
            cost = self._record_failure(run, key, stage, role_name, request.model, result, detail)
            run.event("stage_error", key=key, subtype=result.subtype, errors=result.errors, **cost)
            raise _Stop("failed", f"stage {key} failed: {detail}")

        output = result.structured
        if stage == "implement" and output.get("blocked"):
            # Not memoised: a resume re-runs the stage after the human intervenes.
            run.state["blocked"] = {"key": key, "notes": output.get("notes", "")}
            run.save()
            run.event("stage_blocked", key=key, output=output)
            raise _Stop("needs_human", f"implementer blocked at {key}: {output.get('notes', '')}")
        if apply_guidance:
            blocked["applied"] = True
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
        tree = _snapshot_tree(self.cwd)
        names = _safe_diff(self.cwd, base, tree, "--name-status", check=False)
        stat = _safe_diff(self.cwd, base, tree, "--stat", check=False)
        diff = _safe_diff(self.cwd, base, tree, check=False)
        if len(diff) > DIFF_CAP:
            diff = diff[:DIFF_CAP] + f"\n[... diff truncated at {DIFF_CAP} chars ...]"
        return (
            f"## Changes since base {base}\n"
            f"### git diff --name-status\n{names.strip() or '(none)'}\n"
            f"### git diff --stat\n{stat.strip() or '(no diff)'}\n"
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
            # Revised plans always go back to the gate (approve_plan is not persisted).
            if size == "L" or self.options.approve_plan or run.state.get("plan_revision"):
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
        run = self.run_state
        assert run is not None
        base = "architect" if architect else "plan"
        rev = int(run.state.get("plan_revision", 0))

        def key(n: int) -> str:
            return f"{base}:r{n}" if n else base

        def prompt() -> str:
            context = f"\n\nExplorer findings (JSON):\n{_dumps(explore)}" if explore else ""
            ask = (
                "Write an implementation plan: ordered steps with stable ids and files, "
                "tests, acceptance criteria and risks."
            )
            if rev:
                previous = run.stage(key(rev - 1))
                prev_plan = previous["output"] if previous else None
                feedback = run.state.get("plan_feedback", [])[rev - 1]
                ask = (
                    f"Previous plan (JSON):\n{_dumps(prev_plan)}\n\n"
                    f"The user rejected it with this feedback:\n{feedback}\n\n"
                    "Write a revised implementation plan addressing the feedback: ordered "
                    "steps with stable ids and files, tests, acceptance criteria and risks."
                )
            return f"Task: {task}{context}\n\n{ask}"

        if architect:
            return await self._stage(key(rev), "plan", "architect", "architect", prompt)
        return await self._stage(key(rev), "plan", None, "plan", prompt)

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

        guidance = run.state.get("retry_feedback", {}).get(str(rnd)) if rnd else None
        blocked = run.state.get("blocked_feedback") or {}
        if blocked.get("round") == rnd and blocked.get("applied"):
            guidance = None  # already given to the re-run blocked implement stage
        if guidance:

            def guided_prompt() -> str:
                items = _last_failing(run)
                return (
                    f"Task: {task}\n\nThe run stopped with these failing items (JSON):\n"
                    f"{_dumps(items)}\n\nUser guidance:\n{guidance}\n\n"
                    f"Fix the failing items following the guidance.\n\n{self._diff_context()}"
                )

            fixes.append(
                await self._stage(
                    f"{prefix}guided-implement",
                    "implement",
                    "implementer",
                    "implement",
                    guided_prompt,
                )
            )

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
        attempts = state.get("failed_attempts", [])
        cost_items = [f"{e['key']} ${e['cost_usd']:.2f}" for e in stages]
        cost_items += [
            f"{a['key']} ${a['cost_usd']:.2f} (failed)"
            if a.get("counted")
            else f"{a['key']} cost unknown (failed, uncounted)"
            for a in attempts
        ]
        costs = ", ".join(cost_items) or "none"
        uncounted = sum(1 for a in attempts if not a.get("counted"))
        uncounted_note = (
            f" (+{uncounted} stage attempt{'s' if uncounted != 1 else ''} uncounted)"
            if uncounted
            else ""
        )
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
            f"({'API key' if state.get('use_api_key') else 'subscription login'})"
            f"{uncounted_note}",
            f"run dir: {rel_dir}",
        ]
        if state.get("base_kind"):
            lines.append(f"carcara changes: carcara diff {run.id}")
        if state.get("accepted_failures"):
            lines.append("accepted failures: yes (unresolved failures accepted by user)")
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
    "require_toplevel",
    "run_diff",
]
