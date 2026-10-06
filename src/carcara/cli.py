"""Command-line entry point for carcara."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
from typing import Any

from carcara import __version__

SUBCOMMANDS = ("install", "run", "profiles")
SIZES = ("S", "M", "L")
DEFAULT_PROFILE = "balanced"


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
    sub.add_parser("profiles", help="list available profiles and their model routing")
    _add_run_parser(sub)
    return parser


def _add_run_parser(sub: Any) -> None:
    run = sub.add_parser(
        "run",
        help="run the SDLC pipeline headlessly via the Claude Agent SDK",
        description="Run the triaged SDLC pipeline for TASK. Exit codes: 0 ok, 1 error, "
        "3 awaiting plan approval, 4 needs human, 5 budget exceeded, 130 interrupted.",
    )
    run.add_argument("task", nargs="?", help="task description (omit with --resume/--list)")
    run.add_argument(
        "-p",
        "--profile",
        default=None,
        help=f"profile name or .env path (default {DEFAULT_PROFILE})",
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
    run.add_argument("--resume", metavar="RUN_ID", help="resume a stored run")
    run.add_argument("--list", action="store_true", help="list stored runs and their status")
    run.add_argument("--allow-dirty", action="store_true", help="allow uncommitted changes")
    run.add_argument("--review", action="store_true", help="also review S-sized changes")
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


def _err(msg: str) -> None:
    sys.stderr.write(f"carcara: {msg}\n")


def _dry_run_stages(size: str, review_small: bool) -> list[tuple[str, str | None]]:
    """(stage, role) rows mirroring the orchestrator's pipelines."""
    if size == "S":
        rows = [("implement", "implementer"), ("test", "test-runner")]
        if review_small:
            rows.append(("review", "reviewer"))
        return rows
    plan = ("plan", "architect") if size == "L" else ("plan", None)
    rows = [("explore", "explorer"), plan, ("implement", "implementer")]
    rows += [("test", "test-runner"), ("review", "reviewer")]
    if size == "L":
        rows.append(("docs", "doc-writer"))
    return rows


def _dry_run(profile: Any, ns: argparse.Namespace, cwd: str) -> str:
    from carcara.backend import build_request
    from carcara.roles import get_role

    out = [f"carcara run --dry-run (profile {profile.name}; no backend calls)"]
    for size in (ns.size,) if ns.size else SIZES:
        out.append(f"\nsize {size}:")
        rows: list[tuple[str, str | None]] = [] if ns.size else [("triage", None)]
        rows += _dry_run_stages(size, ns.review)
        out.append(f"  {'stage':<10} {'role':<12} {'model':<24} {'mode':<12} tools")
        for stage, role_name in rows:
            role = get_role(role_name) if role_name else None
            req = build_request(stage, role, profile, "", cwd)
            label = role_name or "main"
            tools = ",".join(req.allowed_tools) or "-"
            out.append(
                f"  {stage:<10} {label:<12} {req.model:<24} {req.permission_mode:<12} {tools}"
            )
            if stage == "plan" and (size == "L" or ns.approve_plan):
                out.append(f"  {'gate':<10} {'human':<12} {'-':<24} {'-':<12} -")
        if size == "L":
            out.append("  (implement runs once per plan step; docs only for user-facing changes)")
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
        lines.append(
            f"{run_id}  {state.get('status', '?'):<17} {state.get('size') or '?'}  "
            f"${cost:.2f}  {task}"
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


def run_main(ns: argparse.Namespace) -> int:
    from carcara.backend import BackendError
    from carcara.orchestrator import Orchestrator, OrchestratorError, RunOptions
    from carcara.profiles import ProfileError, load_profile
    from carcara.roles import RoleError
    from carcara.runstore import RunStore, RunStoreError

    cwd = os.path.abspath(ns.cwd)
    if not os.path.isdir(cwd):
        _err(f"directory not found: {ns.cwd}")
        return 1
    if ns.list:
        sys.stdout.write(_list_runs(cwd))
        return 0

    orch: Orchestrator | None = None
    try:
        if ns.dry_run:
            sys.stdout.write(_dry_run(load_profile(ns.profile or DEFAULT_PROFILE), ns, cwd))
            return 0
        store = RunStore(cwd)
        if ns.resume:
            try:
                state = store.load(ns.resume).state
            except RunStoreError as exc:
                raise OrchestratorError(str(exc)) from exc
            profile = _resume_profile(state, ns.profile)
            use_api_key = ns.use_api_key or bool(state.get("use_api_key"))
        else:
            if not ns.task:
                _err("missing TASK (or use --resume RUN_ID / --list / --dry-run)")
                return 1
            profile = load_profile(ns.profile or DEFAULT_PROFILE)
            use_api_key = ns.use_api_key
        backend = _ProgressBackend(_make_backend(use_api_key))
        options = RunOptions(
            size=ns.size,
            review_small=ns.review,
            approve_plan=ns.approve_plan,
            plan_only=ns.plan_only,
            max_budget_usd=ns.max_budget_usd,
            allow_dirty=ns.allow_dirty,
            project_settings=ns.project_settings,
            use_api_key=use_api_key,
        )
        orch = Orchestrator(backend, profile, cwd, store, _make_gate(ns.yes), options)
        coro = orch.resume(ns.resume) if ns.resume else orch.run(ns.task)
        outcome = asyncio.run(coro)
    except KeyboardInterrupt:
        run = orch.run_state if orch is not None else None
        if run is not None:
            _err(f"interrupted; state saved. Resume with: {_resume_cmd(run.id, ns.cwd)}")
        else:
            _err("interrupted")
        return 130
    except (
        OrchestratorError,
        BackendError,
        ProfileError,
        RoleError,
        RunStoreError,
        OSError,
        ValueError,
    ) as exc:
        _err(str(exc))
        return 1

    sys.stdout.write(outcome.report_text)
    if outcome.status == "awaiting_approval" and outcome.run_id:
        _err(f"plan awaits approval. Resume with: {_resume_cmd(outcome.run_id, ns.cwd)} --yes")
    return outcome.exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Back-compat with the 0.1.0 flag-only interface: anything that is not a
    # subcommand or a top-level -h/-V is an install invocation.
    if not args or (
        args[0] not in SUBCOMMANDS and args[0] not in ("-h", "--help", "-V", "--version")
    ):
        args.insert(0, "install")

    if args[0] == "install":
        from carcara.installer import main as install_main

        return install_main(args[1:])

    parser = build_parser()
    ns = parser.parse_args(args)
    if ns.command == "run":
        return run_main(ns)
    if ns.command == "profiles":
        from carcara.profiles import list_profiles

        sys.stdout.write(list_profiles())
        return 0
    parser.print_help()
    return 0
