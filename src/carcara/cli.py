"""Command-line entry point for carcara."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from carcara import __version__

SUBCOMMANDS = ("install", "uninstall", "run", "status", "diff", "profiles", "hook", "routing")
SIZES = ("S", "M", "L")
DEFAULT_PROFILE = "balanced"
STAGE_ENV_VAR = "CARCARA_STAGE"  # mirrors carcara.backend.STAGE_ENV_VAR (kept import-light)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carcara",
        description="carcara - agentic SDLC framework for Claude Code",
        epilog="Without a subcommand, arguments are passed to `carcara install` "
        "(e.g. `carcara -p quality DIR`).",
    )
    parser.add_argument("-V", "--version", action="version", version=f"carcara {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser(
        "install",
        add_help=False,
        help="install agents, commands and CLAUDE.md section (see `carcara install -h`)",
    )
    sub.add_parser(
        "uninstall",
        add_help=False,
        help="remove what `carcara install` added (see `carcara uninstall -h`)",
    )
    sub.add_parser("profiles", help="list available profiles and their model routing")
    _add_run_parser(sub)
    _add_status_parser(sub)
    _add_diff_parser(sub)
    _add_hook_parsers(sub)
    return parser


def _add_hook_parsers(sub: Any) -> None:
    hook = sub.add_parser(
        "hook",
        allow_abbrev=False,
        help="Claude Code hook handler (JSON on stdin; always exits 0)",
        description="Claude Code hook handler: reads hook JSON on stdin, prints hook JSON.",
    )
    hook.add_argument("name", choices=("pre-tool-use", "prompt-context"))
    hook.add_argument(
        "--project", default=None, metavar="DIR", help="project dir (default $CLAUDE_PROJECT_DIR)"
    )
    routing = sub.add_parser(
        "routing",
        allow_abbrev=False,
        help="turn Claude Code routing through `carcara run` on/off for a project",
    )
    routing.add_argument("action", choices=("on", "off", "status"))
    routing.add_argument("--project", default=".", metavar="DIR", help="project dir (default .)")


def _add_run_parser(sub: Any) -> None:
    run = sub.add_parser(
        "run",
        allow_abbrev=False,
        help="run the SDLC pipeline headlessly via the Claude Agent SDK",
        description="Run the triaged SDLC pipeline for TASK. Exit codes: 0 ok, 1 error, "
        "3 awaiting plan approval, 4 needs human, 5 budget exceeded, 6 another run is "
        "active, 130 interrupted (Ctrl-C or SIGTERM).",
    )
    run.add_argument(
        "task",
        nargs="?",
        help="task description; `-` reads it from stdin (omit with --resume/--list)",
    )
    run.add_argument(
        "-p",
        "--profile",
        default=None,
        help="profile name or .env path (default: profile recorded by `carcara install`, "
        f"else {DEFAULT_PROFILE})",
    )
    run.add_argument("--size", choices=SIZES, help="skip triage and use this size")
    run.add_argument("--yes", action="store_true", help="auto-approve the plan gate")
    run.add_argument("--approve-plan", action="store_true", help="gate M-sized plans too")
    run.add_argument("--plan-only", action="store_true", help="stop after the plan")
    run.add_argument(
        "--dry-run", action="store_true", help="print the stage table without calling the backend"
    )
    run.add_argument(
        "--max-budget-usd",
        type=float,
        default=None,
        metavar="USD",
        help="cap the SDK's estimated cost (API prices; on a subscription this "
        "counts toward plan usage limits, not billed)",
    )
    run.add_argument(
        "--issue",
        type=_issue_number,
        default=None,
        metavar="N",
        help="take the task from GitHub issue N (TASK, if given, adds instructions) and "
        "report the run to Urutau when URUTAU_MCP_TOKEN is set",
    )
    run.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/NAME",
        help="with --issue: the issue's repo (default: the git origin remote)",
    )
    run.add_argument("--no-urutau", action="store_true", help="do not report this run to Urutau")
    run.add_argument("--resume", metavar="RUN_ID", help="resume a stored run")
    run.add_argument(
        "--reject",
        action="store_true",
        help="with --resume: reject the plan awaiting approval (re-plan if --feedback)",
    )
    run.add_argument(
        "--feedback",
        metavar="TEXT",
        default=None,
        help="with --resume: plan feedback (re-plan) or guidance for a needs_human retry; "
        "`-` reads it from stdin",
    )
    run.add_argument(
        "--accept-failures",
        action="store_true",
        help="with --resume: finish a needs_human run, accepting its failures",
    )
    run.add_argument("--list", action="store_true", help="list stored runs and their status")
    run.add_argument("--allow-dirty", action="store_true", help="allow uncommitted changes")
    run.add_argument(
        "--unrestricted-bash",
        action="store_true",
        help="disable the implementer/test-runner Bash deny-list for this invocation only "
        "(not persisted; pass it again with --resume)",
    )
    run.add_argument(
        "--review",
        action="store_true",
        help="also review S-sized changes (review always runs when the verifiability gate fires)",
    )
    run.add_argument(
        "--ultra",
        action="store_true",
        help="fan out exploration and review into parallel stages (higher cost; "
        "persisted for --resume)",
    )
    run.add_argument(
        "--project-settings",
        action="store_true",
        help="load the project's Claude Code settings/CLAUDE.md into each stage",
    )
    run.add_argument(
        "--use-api-key",
        action="store_true",
        help="let the Claude Code CLI use ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN "
        "(pay-per-token API billing) instead of your Claude subscription login",
    )
    run.add_argument("--cwd", default=".", metavar="DIR", help="project directory (default .)")


def _add_status_parser(sub: Any) -> None:
    status = sub.add_parser(
        "status",
        allow_abbrev=False,
        help="show a stored run (default: the active run, else the latest)",
        description="Show a stored run: its report and how to resume it. Defaults to the "
        "active run, else the latest one.",
    )
    status.add_argument("run_id", nargs="?", metavar="RUN_ID", help="run to show")
    status.add_argument("--json", action="store_true", help="print machine-readable JSON")
    status.add_argument("--plan", action="store_true", help="print the run's stored plan")
    status.add_argument("--cwd", default=".", metavar="DIR", help="project directory (default .)")


def _add_diff_parser(sub: Any) -> None:
    diff = sub.add_parser(
        "diff",
        allow_abbrev=False,
        help="show a run's changes since its base (secrets excluded)",
        description="Show the working tree's changes since a run's base, excluding secrets "
        "and .carcara/. Defaults to the active run, else the latest one.",
    )
    diff.add_argument("run_id", nargs="?", metavar="RUN_ID", help="run to diff")
    diff.add_argument("--stat", action="store_true", help="print a diffstat only")
    diff.add_argument("--cwd", default=".", metavar="DIR", help="project directory (default .)")


def _err(msg: str) -> None:
    sys.stderr.write(f"carcara: {msg}\n")


class IssueError(Exception):
    """The GitHub issue or its repo cannot be resolved."""


def _issue_number(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"invalid issue number: {value!r}")
    return n


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ORIGIN_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def _origin_repo(cwd: str = ".") -> str:
    """``owner/name`` parsed from ``git remote get-url origin``."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=cwd, capture_output=True, text=True
        )
    except OSError as exc:
        raise IssueError(f"cannot run git ({exc}); pass --repo owner/name") from exc
    match = _ORIGIN_RE.match(proc.stdout.strip()) if proc.returncode == 0 else None
    if match is None:
        raise IssueError(
            "cannot tell the GitHub repo from the origin remote; pass --repo owner/name"
        )
    return match.group(1)


def _gh_issue(repo: str, n: int) -> tuple[str, str]:
    """(title, body) of GitHub issue ``n`` in ``repo`` via the gh CLI."""
    import subprocess

    if shutil.which("gh") is None:
        raise IssueError(
            "GitHub CLI `gh` not found; install it (https://cli.github.com) and log in"
        )
    cmd = ["gh", "issue", "view", str(n), "-R", repo, "--json", "title,body"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise IssueError(f"cannot run gh: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise IssueError(f"cannot read issue #{n} of {repo}: {detail}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise IssueError(f"unexpected gh output for issue #{n} of {repo}") from exc
    return str(data.get("title") or ""), str(data.get("body") or "")


def _issue_repo(ns: argparse.Namespace, cwd: str) -> str:
    if ns.repo:
        if not _REPO_RE.match(ns.repo):
            raise IssueError(f"invalid --repo {ns.repo!r}; expected owner/name")
        return ns.repo
    return _origin_repo(cwd)


def _issue_task(ns: argparse.Namespace, repo: str) -> str:
    """The task for --issue: the issue title/body, plus TASK (or stdin) as extra instructions."""
    title, body = _gh_issue(repo, ns.issue)
    task = f"GitHub issue #{ns.issue}: {title}\n\n{body}".rstrip()
    extra = sys.stdin.read() if ns.task == "-" else ns.task
    extra = (extra or "").strip()
    if extra:
        task += "\n\nAdditional instructions:\n" + extra
    return task


def _urutau_config(ns: argparse.Namespace) -> Any:
    """The Urutau config when reporting is on (--issue/resume, a token, no --no-urutau)."""
    if ns.no_urutau:
        return None
    from carcara.urutau import load_config

    return load_config()


def _dry_run_stages(
    size: str, review_small: bool, ultra: bool = False
) -> list[tuple[str, str | None]]:
    """(stage, role) rows mirroring the orchestrator's pipelines.

    Ultra rows carry a label after the stage name (e.g. "review x3 ..."); the
    first word is the stage."""
    review = (
        ("review x3 (parallel) + merge + refute x<=6 (parallel)", "reviewer")
        if ultra
        else ("review", "reviewer")
    )
    if size == "S":
        rows = [("implement", "implementer"), ("test", "test-runner")]
        if review_small:
            rows.append(review)
        return rows
    plan = ("plan", "architect") if size == "L" else ("plan", None)
    explore: list[tuple[str, str | None]] = [("explore", "explorer")]
    if ultra:
        explore = [("scope", None), ("explore x<=4 (parallel)", "explorer")]
    rows = [*explore, plan, ("implement", "implementer")]
    rows += [("test", "test-runner"), review]
    if size == "L":
        rows.append(("docs", "doc-writer"))
    return rows


def _project_root(cwd: str) -> str:
    """The git toplevel containing ``cwd``, else ``cwd`` itself."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True
        )
    except OSError:
        return cwd
    top = proc.stdout.strip()
    return top if proc.returncode == 0 and top else cwd


def _select_profile(ns: argparse.Namespace, cwd: str) -> tuple[Any, str]:
    """(profile, origin): --profile, else the one `carcara install` recorded, else balanced."""
    from carcara.profiles import ProfileError, load_profile, read_installed_profile

    if ns.profile:
        return load_profile(ns.profile), "explicit"
    spec = read_installed_profile(_project_root(cwd))
    if spec is None:
        return load_profile(DEFAULT_PROFILE), "default"
    try:
        return load_profile(spec), "installed"
    except (ProfileError, OSError) as exc:
        _err(
            f"warning: installed profile {spec!r} could not be loaded ({exc}); "
            f"falling back to {DEFAULT_PROFILE}"
        )
        return load_profile(DEFAULT_PROFILE), "default"


def _dry_run(profile: Any, ns: argparse.Namespace, cwd: str, origin: str) -> str:
    from carcara.backend import build_request
    from carcara.roles import get_role

    out = [f"carcara run --dry-run (profile {profile.name}, {origin}; no backend calls)"]
    if ns.issue is not None:
        try:
            repo = _issue_repo(ns, cwd)
        except IssueError:
            repo = "? (pass --repo owner/name)"
        on = "on" if _urutau_config(ns) is not None else "off"
        out.append(f"issue #{ns.issue} ({repo}); Urutau reporting: {on}")
    for size in (ns.size,) if ns.size else SIZES:
        out.append(f"\nsize {size}:")
        rows: list[tuple[str, str | None]] = [] if ns.size else [("triage", None)]
        rows += _dry_run_stages(size, ns.review, ns.ultra)
        width = max(10, *(len(stage) for stage, _ in rows))
        out.append(f"  {'stage':<{width}} {'role':<12} {'model':<24} {'mode':<12} tools")
        for stage, role_name in rows:
            role = get_role(role_name) if role_name else None
            req = build_request(stage.split()[0], role, profile, "", cwd)
            label = role_name or "main"
            tools = ",".join(req.allowed_tools) or "-"
            out.append(
                f"  {stage:<{width}} {label:<12} {req.model:<24} {req.permission_mode:<12} {tools}"
            )
            if stage == "plan" and (size == "L" or ns.approve_plan):
                out.append(f"  {'gate':<{width}} {'human':<12} {'-':<24} {'-':<12} -")
        if size == "S" and not ns.review:
            out.append("  (review runs only if the verifiability gate fires)")
        if size == "L":
            out.append("  (implement runs once per plan step; docs only for user-facing changes)")
    out.append(
        "\n(the gate also triggers when the plan or, for S, the changed files touch"
        " low-verifiability paths; see .carcara/config.json verifiability_paths)"
    )
    return "\n".join(out) + "\n"


def _list_runs(cwd: str) -> str:
    from carcara.runstore import RunStore, RunStoreError

    store = RunStore(cwd)
    lines = []
    for run_id in store.list_runs():
        try:
            state = store.load(run_id).state
        except RunStoreError:
            lines.append(f"{run_id}  unreadable")
            continue
        task = " ".join(str(state.get("task", "")).split())
        task = task if len(task) <= 60 else task[:57] + "..."
        cost = float(state.get("totals", {}).get("cost_usd", 0.0))
        unknown = "+?" if state.get("totals", {}).get("uncounted_stages") else ""
        lines.append(
            f"{run_id}  {state.get('status', '?'):<17} {state.get('size') or '?'}  "
            f"${cost:.2f}{unknown}  {task}"
        )
    return "\n".join(lines) + "\n" if lines else "no runs\n"


class _ProgressBackend:
    """Wraps a backend to print per-stage progress lines to stderr."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def run_stage(self, request: Any) -> Any:
        sys.stderr.write(
            f"carcara: {request.stage} ({request.role or 'main'}, {request.model}) ...\n"
        )
        sys.stderr.flush()
        result = await self.inner.run_stage(request)
        sys.stderr.write(f"carcara: {request.stage} done (${result.cost_usd:.2f})\n")
        return result


def _make_backend(use_api_key: bool = False) -> Any:
    from carcara.backend import API_KEY_ENV_VARS, FakeBackend, SdkBackend

    # TEST-ONLY hook: CARCARA_BACKEND=fake:<script.json> replays scripted stage
    # outputs ({"script": {stage: [output, ...]}, "costs": {stage: usd}}) instead
    # of calling Claude. Not a supported user feature.
    spec = os.environ.get("CARCARA_BACKEND", "")
    if spec.startswith("fake:"):
        with open(spec[len("fake:") :], encoding="utf-8") as fh:
            data = json.load(fh)
        return FakeBackend(data.get("script", {}), data.get("costs"))
    if shutil.which("claude") is None:
        from carcara.backend import BackendUnavailable

        raise BackendUnavailable(
            "Claude Code CLI not found; install it "
            "(https://code.claude.com/docs/en/setup) and make sure `claude` is on PATH"
        )
    if use_api_key:
        _err("using API key billing (pay-per-token)")
    else:
        for name in API_KEY_ENV_VARS:
            if os.environ.get(name):
                _err(
                    f"ignoring {name}; using your Claude Code login "
                    "(pass --use-api-key to bill the API)"
                )
                break
    return SdkBackend(use_api_key=use_api_key)


def _make_gate(yes: bool) -> Any:
    from carcara.gate import NonInteractiveGate, TtyGate
    from carcara.orchestrator import AutoGate

    if yes:
        return AutoGate(approve=True, continue_=False)
    if os.environ.get("CLAUDECODE"):
        # Driven from a Claude Code session: never block on stdin.
        return NonInteractiveGate()
    try:
        tty = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        tty = False
    return TtyGate(stdout=sys.stderr) if tty else NonInteractiveGate()


def _resume_cmd(run_id: str, cwd_arg: str) -> str:
    cwd_part = "" if os.path.abspath(cwd_arg) == os.getcwd() else f" --cwd {shlex.quote(cwd_arg)}"
    return f"carcara run --resume {run_id}{cwd_part}"


def _resume_profile(state: dict[str, Any], explicit: str | None) -> Any:
    from carcara.profiles import load_profile

    source = state.get("profile_source")
    stored = state["profile"]
    profile = load_profile(source if source and os.path.isfile(source) else stored)
    if explicit is not None and load_profile(explicit).name != stored:
        _err(f"warning: run uses profile {stored}; ignoring --profile {explicit}")
    return profile


def _stored_plan(state: dict[str, Any]) -> dict[str, Any] | None:
    """The latest plan stage output (``plan``/``architect`` and revisions)."""
    plans = [e["output"] for e in state.get("stages", []) if e.get("stage") == "plan"]
    return plans[-1] if plans else None


def _failing(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Latest failing tests, else blocker/major findings of the review after them."""
    stages = state.get("stages", [])
    last_test = max((i for i, e in enumerate(stages) if e.get("stage") == "test"), default=None)
    if last_test is None:
        return []
    test = stages[last_test]["output"]
    if not test.get("passed"):
        return [{"kind": "test", **f} for f in test.get("failures", [])]
    for entry in stages[last_test + 1 :]:
        if entry.get("stage") == "review":
            return [
                {"kind": "review", **f}
                for f in entry["output"].get("findings", [])
                if f.get("severity") in ("blocker", "major")
            ]
    return []


def _status_resume_cmd(run_id: str, status: str, active: bool, cwd_arg: str) -> str | None:
    base = _resume_cmd(run_id, cwd_arg)
    if status == "done" or active:
        return None
    if status == "awaiting_approval":
        return f"{base} --yes"
    if status == "needs_human":
        return f'{base} --feedback "..."'
    return base


def _pick_run(ns: argparse.Namespace) -> tuple[Any, Any, dict[str, Any] | None] | None:
    """(store, run, live lock holder) for ``ns.run_id``, else the active, else the
    latest run. Prints an error and returns None when there is none."""
    from carcara.runstore import RunStore, RunStoreError

    cwd = os.path.abspath(ns.cwd)
    if not os.path.isdir(cwd):
        _err(f"directory not found: {ns.cwd}")
        return None
    store = RunStore(cwd)
    holder = store.active()
    run_id = ns.run_id
    if not run_id:
        runs = store.list_runs()
        if holder is not None and holder["run_id"] in runs:
            run_id = holder["run_id"]
        elif runs:
            run_id = runs[-1]
        else:
            _err("no runs")
            return None
    try:
        run = store.load(run_id)
    except RunStoreError as exc:
        _err(str(exc))
        return None
    return store, run, holder


def diff_main(ns: argparse.Namespace) -> int:
    from carcara.orchestrator import OrchestratorError, require_toplevel, run_diff

    if os.path.isdir(ns.cwd):
        try:
            require_toplevel(os.path.abspath(ns.cwd))
        except OrchestratorError as exc:
            _err(str(exc))
            return 1
    picked = _pick_run(ns)
    if picked is None:
        return 1
    store, run, _ = picked
    base = run.state.get("base_sha")
    if not isinstance(base, str) or not base:
        _err(f"run {run.id} has no base commit")
        return 1
    try:
        sys.stdout.write(run_diff(str(store.cwd), base, stat=ns.stat))
    except OrchestratorError as exc:
        _err(str(exc))
        return 1
    return 0


def _verifiability_lines(state: dict[str, Any]) -> list[str]:
    """Gate trigger, extent facts and open unverified items for the no-report fallback."""
    from carcara.schemas import UNVERIFIED_KINDS

    lines = []
    gate = state.get("gate")
    if gate:
        paths = f" (paths: {', '.join(gate['paths'])})" if gate.get("paths") else ""
        lines.append(f"gate: {gate.get('trigger', '?')}{paths}")
    if state.get("review_reason"):
        lines.append(f"review: forced by {state['review_reason']}")
    extent = state.get("extent")
    if extent:
        lines.append(
            f"extent: {extent.get('files_changed', 0)} files, "
            f"areas {', '.join(extent.get('areas') or []) or 'none'}, "
            f"fix rounds {extent.get('fix_rounds', 0)} [{extent.get('rule', '?')}]"
        )
    open_items = [i for i in state.get("unverified") or [] if not i.get("resolved")]
    if open_items:
        counts = ", ".join(
            f"{kind} {sum(1 for i in open_items if i.get('kind') == kind)}"
            for kind in UNVERIFIED_KINDS
        )
        lines.append(f"unverified: {len(open_items)} open ({counts})")
        lines.extend(f"  - {i['id']} [{i['kind']}] {i['text']}" for i in open_items)
    return lines + _issue_lines(state)


def _issue_lines(state: dict[str, Any]) -> list[str]:
    """Triage range/uncertainty and the --issue/Urutau lines (never the token)."""
    from carcara.orchestrator import _urutau_report_lines

    lines = []
    if state.get("triage_range") is not None or state.get("uncertainty_kind") is not None:
        lines.append(
            f"Triage range: {state.get('triage_range') or '?'}; "
            f"uncertainty: {state.get('uncertainty_kind') or '?'}"
        )
    elif (state.get("issue") is not None or (state.get("urutau") or {}).get("enabled")) and (
        state.get("size") and not any(e.get("stage") == "triage" for e in state.get("stages", []))
    ):
        lines.append("Triage range: n/a (size forced)")
    return lines + _urutau_report_lines(state)


def status_main(ns: argparse.Namespace) -> int:
    from carcara.gate import render_plan
    from carcara.orchestrator import EXIT_CODES

    picked = _pick_run(ns)
    if picked is None:
        return 1
    _, run, holder = picked
    state = run.state
    status = str(state.get("status", "?"))
    active = holder is not None and holder["run_id"] == run.id
    plan = _stored_plan(state)
    resume_cmd = _status_resume_cmd(run.id, status, active, ns.cwd)
    report = run.read_report()
    attempts = state.get("failed_attempts") or []
    uncounted = [a.get("key") for a in attempts if not a.get("counted")]
    urutau = state.get("urutau") or {}
    if ns.json:
        data = {
            "run_id": run.id,
            "status": status,
            "size": state.get("size"),
            "exit_code": EXIT_CODES.get(status),
            "message": state.get("message"),
            # report.md may be stale (from a previous drive) while a run is in progress.
            "report": None if status == "running" else report,
            "plan": plan,
            "failing": _failing(state) if status == "needs_human" else [],
            "resume_cmd": resume_cmd,
            "active": active,
            "cost_usd": float(state.get("totals", {}).get("cost_usd", 0.0)),
            "failed_attempts": len(attempts),
            "uncounted_stages": uncounted,
            "gate": state.get("gate"),
            "review_reason": state.get("review_reason"),
            "unverified": state.get("unverified") or [],
            "probe_results": state.get("probe_results") or {},
            "extent": state.get("extent"),
            "triage_range": state.get("triage_range"),
            "uncertainty_kind": state.get("uncertainty_kind"),
            "issue": state.get("issue"),
            "card_estimate": state.get("card_estimate"),
            "urutau": {
                "enabled": bool(urutau.get("enabled")),
                "repo": urutau.get("repo"),
                "issue": urutau.get("issue"),
                "last": urutau.get("last"),
            },
        }
        sys.stdout.write(json.dumps(data, indent=2) + "\n")
        return 0
    if ns.plan:
        if plan is None:
            _err(f"run {run.id} has no plan")
            return 1
        sys.stdout.write(render_plan(plan))
        return 0
    if report is None or status == "running":
        # report.md may be stale (from a previous drive) while a run is in progress.
        lines = [f"carcara run {run.id}: {status} (size {state.get('size') or '?'})"]
        if state.get("message"):
            lines.append(f"note: {state['message']}")
        if uncounted:
            lines.append(
                f"warning: cost unknown for {len(uncounted)} failed stage attempt(s): "
                f"{', '.join(map(str, uncounted))}"
            )
        lines += _verifiability_lines(state)
        report = "\n".join(lines) + "\n"
    sys.stdout.write(report)
    if holder is not None and active:
        sys.stdout.write(f"active: running in pid {holder['pid']}\n")
    if resume_cmd:
        sys.stdout.write(f"resume: {resume_cmd}\n")
    return 0


def _raise_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


@contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    """Turn SIGTERM into KeyboardInterrupt so the run saves state and exits 130."""
    try:
        previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    except (ValueError, OSError):  # not the main thread / unsupported
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _announce(run_id: str, resumed: bool, profile_name: str) -> None:
    _err(f"run {run_id} {'resumed' if resumed else 'started'} (profile {profile_name})")
    sys.stderr.flush()


def _check_resume_flags(ns: argparse.Namespace) -> str | None:
    """Status-independent checks of --reject/--feedback/--accept-failures.

    Reads ``--feedback -`` from stdin. Returns an error message or None.
    """
    if not (ns.reject or ns.feedback is not None or ns.accept_failures):
        return None
    if not ns.resume:
        return "--reject, --feedback and --accept-failures require --resume RUN_ID"
    if ns.reject and ns.yes:
        return "--yes cannot be combined with --reject"
    if ns.accept_failures and (ns.reject or ns.feedback is not None):
        return "--accept-failures cannot be combined with --reject or --feedback"
    if ns.feedback == "-":
        ns.feedback = sys.stdin.read()
    if ns.feedback is not None:
        ns.feedback = ns.feedback.strip()
        if not ns.feedback:
            return "empty --feedback"
    return None


async def _with_client(coro: Any, client: Any) -> Any:
    """Await ``coro`` and close the Urutau client in the same task (anyio scopes)."""
    try:
        return await coro
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # never lose the run's result over a close
                _err(f"warning: closing the Urutau connection failed ({type(exc).__name__})")


def run_main(ns: argparse.Namespace) -> int:
    with _sigterm_as_interrupt():
        return _run_main(ns)


def _run_main(ns: argparse.Namespace) -> int:
    from carcara.backend import BackendError
    from carcara.orchestrator import (
        EXIT_CODES,
        Orchestrator,
        OrchestratorError,
        RunOptions,
        require_toplevel,
    )
    from carcara.profiles import ProfileError
    from carcara.roles import RoleError
    from carcara.runstore import RunBusy, RunStore, RunStoreError
    from carcara.urutau import UrutauClient, UrutauError

    if os.environ.get(STAGE_ENV_VAR):
        _err("nested carcara run inside a carcara stage is not allowed")
        return 1
    flag_error = _check_resume_flags(ns)
    if flag_error:
        _err(flag_error)
        return 1
    if ns.issue is not None and ns.resume:
        _err("--issue cannot be combined with --resume (the run keeps its issue)")
        return 1
    if ns.repo and ns.issue is None:
        _err("--repo requires --issue N")
        return 1
    cwd = os.path.abspath(ns.cwd)
    if not os.path.isdir(cwd):
        _err(f"directory not found: {ns.cwd}")
        return 1
    if ns.list:
        sys.stdout.write(_list_runs(cwd))
        return 0

    orch: Orchestrator | None = None
    client: UrutauClient | None = None
    repo: str | None = None
    try:
        if ns.dry_run:
            profile, origin = _select_profile(ns, cwd)
            sys.stdout.write(_dry_run(profile, ns, cwd, origin))
            return 0
        store = RunStore(cwd)
        if ns.resume:
            # Before loading: a subdirectory has no run store of its own.
            require_toplevel(cwd)
            try:
                state = store.load(ns.resume).state
            except RunStoreError as exc:
                raise OrchestratorError(str(exc)) from exc
            if ns.yes and ns.feedback and state.get("status") == "awaiting_approval":
                _err("--yes cannot be combined with --feedback on a plan awaiting approval")
                return 1
            profile = _resume_profile(state, ns.profile)
            use_api_key = ns.use_api_key or bool(state.get("use_api_key"))
            stored = state.get("urutau") or {}
            if stored.get("enabled") and stored.get("repo") and stored.get("issue") is not None:
                cfg = _urutau_config(ns)
                if cfg is not None:
                    client = UrutauClient(cfg, str(stored["repo"]), int(stored["issue"]))
        else:
            if ns.issue is not None:
                repo = _issue_repo(ns, cwd)
                ns.task = _issue_task(ns, repo)
                cfg = _urutau_config(ns)
                if cfg is not None:
                    client = UrutauClient(cfg, repo, ns.issue)
            elif ns.task == "-":
                ns.task = sys.stdin.read().strip()
                if not ns.task:
                    _err("empty task on stdin")
                    return 1
            if not ns.task:
                _err("missing TASK (or use --resume RUN_ID / --list / --dry-run)")
                return 1
            profile, _ = _select_profile(ns, cwd)
            use_api_key = ns.use_api_key
        backend = _ProgressBackend(_make_backend(use_api_key))
        options = RunOptions(
            size=ns.size,
            review_small=ns.review,
            ultra=ns.ultra,
            approve_plan=ns.approve_plan,
            plan_only=ns.plan_only,
            max_budget_usd=ns.max_budget_usd,
            allow_dirty=ns.allow_dirty,
            project_settings=ns.project_settings,
            use_api_key=use_api_key,
            unrestricted_bash=ns.unrestricted_bash,
            issue=ns.issue,
            repo=repo,
        )
        if ns.unrestricted_bash:
            _err(
                "warning: --unrestricted-bash: Bash deny-list disabled for "
                "implementer/test-runner stages"
            )
        name = profile.name
        orch = Orchestrator(
            backend,
            profile,
            cwd,
            store,
            _make_gate(ns.yes),
            options,
            on_start=lambda run_id, resumed: _announce(run_id, resumed, name),
            urutau=client,
        )
        coro = (
            orch.resume(
                ns.resume,
                reject=ns.reject,
                feedback=ns.feedback,
                accept_failures=ns.accept_failures,
            )
            if ns.resume
            else orch.run(ns.task)
        )
        outcome = asyncio.run(_with_client(coro, client))
    except KeyboardInterrupt:
        run = orch.run_state if orch is not None else None
        if run is not None:
            # Normally released by the orchestrator's finally; owner-checked, idempotent.
            RunStore(cwd).release_lock(run.id)
            _err(f"interrupted; state saved. Resume with: {_resume_cmd(run.id, ns.cwd)}")
        else:
            _err("interrupted")
        return 130
    except RunBusy as exc:
        _err(f"another run is active: {exc.run_id} (carcara status {exc.run_id})")
        return EXIT_CODES["busy"]
    except (
        OrchestratorError,
        BackendError,
        ProfileError,
        RoleError,
        RunStoreError,
        UrutauError,
        IssueError,
        OSError,
        ValueError,
    ) as exc:
        _err(str(exc))
        return 1

    sys.stdout.write(outcome.report_text)
    if outcome.status == "awaiting_approval" and outcome.run_id:
        cmd = _resume_cmd(outcome.run_id, ns.cwd)
        _err(f"plan awaits approval. Resume with: {cmd} --yes (or {cmd} --feedback TEXT to revise)")
    return outcome.exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Back-compat with the 0.1.0 flag-only interface: anything that is not a
    # subcommand or a top-level -h/-V is an install invocation.
    if not args or (
        args[0] not in SUBCOMMANDS and args[0] not in ("-h", "--help", "-V", "--version")
    ):
        args.insert(0, "install")

    if args[0] == "hook":
        # Before argparse: hook handlers must exit 0 even on bad arguments.
        from carcara.hooks import hook_main

        return hook_main(args[1:])

    if args[0] == "install":
        from carcara.installer import main as install_main

        return install_main(args[1:])

    if args[0] == "uninstall":
        from carcara.installer import uninstall_main

        return uninstall_main(args[1:])

    parser = build_parser()
    ns = parser.parse_args(args)
    if ns.command == "run":
        return run_main(ns)
    if ns.command == "status":
        return status_main(ns)
    if ns.command == "diff":
        return diff_main(ns)
    if ns.command == "routing":
        from carcara.hooks import routing_main

        return routing_main(ns.action, ns.project)
    if ns.command == "profiles":
        from carcara.profiles import list_profiles

        sys.stdout.write(list_profiles())
        return 0
    parser.print_help()
    return 0
