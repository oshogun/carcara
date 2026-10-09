import io
import json
import os
import subprocess
import sys

import pytest

from carcara import cli
from carcara.cli import main
from carcara.gate import NonInteractiveGate, TtyGate
from carcara.profiles import load_profile
from carcara.roles import get_role, model_for
from carcara.runstore import RunStore

EXPLORE = {"summary": "found", "findings": [{"path": "a.py", "line": 1, "fact": "x"}]}
PLAN = {
    "goal": "do the thing",
    "steps": [{"id": "step-1", "files": ["a.py"], "change": "edit a"}],
    "tests": ["pytest"],
    "acceptance": ["works"],
    "risks": ["none"],
}
IMPL = {
    "changed": [{"path": "a.py", "summary": "s"}],
    "verified": "pytest",
    "notes": "",
    "blocked": False,
    "user_facing_change": False,
}
TEST_OK = {"passed": True, "commands": ["pytest"], "failures": []}
TEST_FAIL = {"passed": False, "commands": ["pytest"], "failures": [{"name": "t1", "detail": "x"}]}
REVIEW_OK = {"verdict": "approve", "findings": [], "unverified": []}


@pytest.fixture(autouse=True)
def _no_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "test@example.com")
    path = tmp_path / "repo"
    path.mkdir()
    (path / "a.py").write_text("x = 1\n")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    return path


@pytest.fixture
def fake(tmp_path, monkeypatch):
    counter = iter(range(1000))

    def use(script, costs=None):
        path = tmp_path / f"script-{next(counter)}.json"
        path.write_text(json.dumps({"script": script, "costs": costs or {}}))
        monkeypatch.setenv("CARCARA_BACKEND", f"fake:{path}")

    return use


def run(repo, *args):
    return main(["run", *args, "--cwd", str(repo)])


def test_dry_run_lists_models_without_backend(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CARCARA_BACKEND", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert run(repo, "--dry-run", "--profile", "quality") == 0
    out = capsys.readouterr().out
    profile = load_profile("quality")
    for size in ("S", "M", "L"):
        assert f"size {size}:" in out
    for role in ("explorer", "architect", "implementer", "test-runner", "reviewer", "doc-writer"):
        line = next(ln for ln in out.splitlines() if f" {role} " in ln)
        assert model_for(get_role(role), profile) in line
    triage = next(ln for ln in out.splitlines() if ln.strip().startswith("triage"))
    assert model_for("main", profile) in triage
    assert "dontAsk" in triage and "acceptEdits" in out
    assert not (repo / ".carcara").exists()


def test_dry_run_single_size(repo, capsys):
    assert run(repo, "--dry-run", "--size", "S") == 0
    out = capsys.readouterr().out
    assert "size S:" in out and "size M:" not in out and "triage" not in out


def test_dry_run_ultra_shows_parallel_rows(repo, capsys):
    assert run(repo, "--dry-run", "--size", "L", "--ultra") == 0
    out = capsys.readouterr().out
    stages = [ln.split()[0] for ln in out.splitlines() if ln.startswith("  ")]
    assert stages[:3] == ["stage", "scope", "explore"]
    assert "explore x<=4 (parallel)" in out and "review x3 (parallel) + merge" in out
    scope = next(ln for ln in out.splitlines() if ln.strip().startswith("scope"))
    assert " main " in scope


def test_dry_run_without_ultra_has_no_parallel_rows(repo, capsys):
    assert run(repo, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "scope" not in out and "parallel" not in out


def test_dry_run_ultra_rows_stay_aligned(repo, capsys):
    assert run(repo, "--dry-run", "--size", "L", "--ultra") == 0
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ")]
    rows = [ln for ln in rows if not ln.startswith("  (")]
    width = len("review x3 (parallel) + merge")
    assert {ln[2 + width] for ln in rows} == {" "}
    assert {ln[3 + width] for ln in rows} != {" "}


def test_dry_run_without_ultra_keeps_ten_char_stage_column(repo, capsys):
    assert run(repo, "--dry-run") == 0
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ")]
    assert "  stage      role         model" in rows[0]
    assert all(ln[12] == " " and ln[13] != " " for ln in rows if not ln.startswith("  ("))


def test_dry_run_ultra_small_review(repo, capsys):
    assert run(repo, "--dry-run", "--size", "S", "--ultra", "--review") == 0
    out = capsys.readouterr().out
    assert "review x3 (parallel) + merge" in out and "scope" not in out


def test_m_run_succeeds(repo, fake, capsys):
    fake(
        {
            "explore": [EXPLORE],
            "plan": [PLAN],
            "implement": [IMPL],
            "test": [TEST_OK],
            "review": [REVIEW_OK],
        },
        {"implement": 0.25},
    )
    assert run(repo, "add a feature", "--size", "M") == 0
    captured = capsys.readouterr()
    assert ": done (size M)" in captured.out
    assert "tests: passed" in captured.out
    assert "carcara: explore (explorer," in captured.err
    assert "carcara: implement done ($0.25)" in captured.err


def test_ultra_run_end_to_end(repo, fake, capsys):
    scope = {"areas": [{"id": a, "focus": a} for a in ("a", "b")], "rationale": "r"}
    fake(
        {
            "scope": [scope],
            "explore:a": [EXPLORE],
            "explore:b": [EXPLORE],
            "plan": [PLAN],
            "implement": [IMPL],
            "test": [TEST_OK],
            "review-dim": [REVIEW_OK] * 3,
            "review": [REVIEW_OK],
        }
    )
    assert run(repo, "add a feature", "--size", "M", "--ultra") == 0
    (run_id,) = RunStore(repo).list_runs()
    state = _state(repo, run_id)
    assert state["ultra"] is True
    assert [e["key"] for e in state["stages"]][:3] == ["scope", "explore:a", "explore:b"]


def test_l_non_tty_defers_then_resume_with_yes(repo, fake, capsys):
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    captured = capsys.readouterr()
    assert "awaiting_approval" in captured.out
    (run_id,) = RunStore(repo).list_runs()
    assert f"carcara run --resume {run_id}" in captured.err

    # No explore/plan entries: re-running them would exhaust the script and fail.
    fake({"implement": [IMPL], "test": [TEST_OK], "review": [REVIEW_OK]})
    assert run(repo, "--resume", run_id, "--yes") == 0
    captured = capsys.readouterr()
    assert ": done (size L)" in captured.out
    assert "carcara: explore" not in captured.err

    assert run(repo, "--list") == 0
    listing = capsys.readouterr().out
    assert run_id in listing and "done" in listing and "big change" in listing


def test_resume_warns_on_profile_mismatch(repo, fake, capsys):
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L", "--profile", "economy") == 3
    (run_id,) = RunStore(repo).list_runs()
    fake({"implement": [IMPL], "test": [TEST_OK], "review": [REVIEW_OK]})
    capsys.readouterr()
    assert run(repo, "--resume", run_id, "--yes", "--profile", "quality") == 0
    err = capsys.readouterr().err
    assert "run uses profile economy" in err
    assert model_for(get_role("implementer"), load_profile("economy")) in err


def test_needs_human_exit_4(repo, fake, capsys):
    fake({"implement": [IMPL, IMPL, IMPL], "test": [TEST_FAIL, TEST_FAIL, TEST_FAIL]})
    assert run(repo, "small fix", "--size", "S", "--yes") == 4
    assert "needs_human" in capsys.readouterr().out


def test_budget_exit_5(repo, fake, capsys):
    fake({"explore": [EXPLORE], "plan": [PLAN]}, {"explore": 1.0})
    assert run(repo, "x", "--size", "M", "--max-budget-usd", "0.5") == 5
    assert "budget_exceeded" in capsys.readouterr().out


def test_missing_claude_cli(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CARCARA_BACKEND", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert run(repo, "anything") == 1
    assert "carcara: Claude Code CLI not found" in capsys.readouterr().err


def test_missing_task_and_unknown_run(repo, fake, capsys):
    fake({})
    assert run(repo) == 1
    assert "missing TASK" in capsys.readouterr().err
    assert run(repo, "--resume", "nope") == 1
    assert "carcara: unknown run: nope" in capsys.readouterr().err


def test_list_empty(repo, capsys):
    assert run(repo, "--list") == 0
    assert capsys.readouterr().out == "no runs\n"


def test_install_back_compat(tmp_path, capsys):
    target = tmp_path / "proj"
    target.mkdir()
    assert main(["-n", str(target)]) == 0
    assert "CLAUDE.md" in capsys.readouterr().out
    assert not (target / ".claude").exists()


def test_tty_gate():
    out = io.StringIO()
    assert TtyGate(io.StringIO("y\n"), out).approve_plan(PLAN) == "approve"
    rendered = out.getvalue()
    assert "Plan: do the thing" in rendered and "files: a.py" in rendered
    assert "Risks:" in rendered and "Approve plan? [y/N/d(efer)]" in rendered
    assert rendered.startswith("Plan: do the thing")
    out = io.StringIO()
    TtyGate(io.StringIO("y\n"), out).approve_plan({**PLAN, "gate_reason": "size L"})
    assert out.getvalue().startswith("Gate: size L\nPlan: do the thing\n")
    assert TtyGate(io.StringIO("d\n"), io.StringIO()).approve_plan(PLAN) == "defer"
    assert TtyGate(io.StringIO(""), io.StringIO()).approve_plan(PLAN) == "reject"
    assert TtyGate(io.StringIO("y\n"), io.StringIO()).ask_continue("s") is True
    assert TtyGate(io.StringIO("\n"), io.StringIO()).ask_continue("s") is False
    gate = NonInteractiveGate()
    assert gate.approve_plan(PLAN) == "defer" and gate.ask_continue("s") is False


def test_keyboard_interrupt_exit_130(repo, fake, monkeypatch, capsys):
    def boom(self, plan):
        raise KeyboardInterrupt

    monkeypatch.setattr(NonInteractiveGate, "approve_plan", boom)
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 130
    (run_id,) = RunStore(repo).list_runs()
    assert f"Resume with: carcara run --resume {run_id}" in capsys.readouterr().err
    assert RunStore(repo).load(run_id).stage("explore") is not None


@pytest.mark.parametrize(
    "exc",
    ["carcara.roles.RoleError", "carcara.runstore.RunStoreError"],
)
def test_carcara_errors_exit_1(repo, fake, monkeypatch, capsys, exc):
    import importlib

    module, name = exc.rsplit(".", 1)
    error = getattr(importlib.import_module(module), name)

    async def boom(self, task):
        raise error("bad thing")

    monkeypatch.setattr("carcara.orchestrator.Orchestrator.run", boom)
    fake({})
    assert run(repo, "anything") == 1
    assert "carcara: bad thing" in capsys.readouterr().err


@pytest.fixture
def real_backend_env(monkeypatch):
    monkeypatch.delenv("CARCARA_BACKEND", raising=False)
    monkeypatch.setattr("carcara.cli.shutil.which", lambda name: "/usr/bin/claude")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_make_backend_ignores_api_key_by_default(real_backend_env, monkeypatch, capsys):
    from carcara.cli import _make_backend

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    backend = _make_backend()
    assert backend.use_api_key is False
    assert capsys.readouterr().err == (
        "carcara: ignoring ANTHROPIC_API_KEY; using your Claude Code login "
        "(pass --use-api-key to bill the API)\n"
    )


def test_make_backend_silent_without_key(real_backend_env, capsys):
    from carcara.cli import _make_backend

    assert _make_backend().use_api_key is False
    assert capsys.readouterr().err == ""


def test_make_backend_use_api_key_notice(real_backend_env, monkeypatch, capsys):
    from carcara.cli import _make_backend

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    assert _make_backend(use_api_key=True).use_api_key is True
    assert capsys.readouterr().err == "carcara: using API key billing (pay-per-token)\n"


def test_dry_run_has_no_billing_notice(repo, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert run(repo, "--dry-run", "--size", "S") == 0
    assert capsys.readouterr().err == ""


def _state(repo, run_id):
    return RunStore(repo).load(run_id).state


def test_use_api_key_persists_across_resume(repo, fake, monkeypatch):
    seen = []
    real = cli._make_backend

    def spy(use_api_key=False):
        seen.append(use_api_key)
        return real(use_api_key)

    monkeypatch.setattr("carcara.cli._make_backend", spy)
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L", "--use-api-key") == 3
    (run_id,) = RunStore(repo).list_runs()
    assert _state(repo, run_id)["use_api_key"] is True
    fake({"implement": [IMPL], "test": [TEST_OK], "review": [REVIEW_OK]})
    assert run(repo, "--resume", run_id, "--yes") == 0
    assert _state(repo, run_id)["use_api_key"] is True
    assert seen == [True, True]


def test_use_api_key_enabled_on_resume(repo, fake, monkeypatch):
    seen = []
    real = cli._make_backend

    def spy(use_api_key=False):
        seen.append(use_api_key)
        return real(use_api_key)

    monkeypatch.setattr("carcara.cli._make_backend", spy)
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    (run_id,) = RunStore(repo).list_runs()
    assert _state(repo, run_id)["use_api_key"] is False
    fake({"implement": [IMPL], "test": [TEST_OK], "review": [REVIEW_OK]})
    assert run(repo, "--resume", run_id, "--yes", "--use-api-key") == 0
    assert _state(repo, run_id)["use_api_key"] is True
    assert seen == [False, True]


def test_nested_run_inside_stage_refused(repo, fake, monkeypatch, capsys):
    monkeypatch.setenv("CARCARA_STAGE", "implementer")
    fake({})
    assert run(repo, "anything") == 1
    assert (
        capsys.readouterr().err
        == "carcara: nested carcara run inside a carcara stage is not allowed\n"
    )
    assert not (repo / ".carcara").exists()


class _FakeTty(io.StringIO):
    def isatty(self):
        return True


def test_make_gate_tty_vs_claudecode(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeTty())
    assert isinstance(cli._make_gate(False), TtyGate)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert isinstance(cli._make_gate(False), NonInteractiveGate)
    assert not isinstance(cli._make_gate(True), (TtyGate, NonInteractiveGate))


def test_claudecode_l_run_defers_even_with_tty(repo, fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _FakeTty("y\n"))
    monkeypatch.setenv("CLAUDECODE", "1")
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    assert "awaiting_approval" in capsys.readouterr().out
    assert sys.stdin.read() == "y\n"  # the gate never read stdin


def test_task_from_stdin(repo, fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("  add a feature\nwith details\n\n"))
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "-", "--size", "S") == 0
    (run_id,) = RunStore(repo).list_runs()
    assert RunStore(repo).load(run_id).state["task"] == "add a feature\nwith details"


def test_empty_task_from_stdin(repo, fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
    fake({})
    assert run(repo, "-", "--size", "S") == 1
    assert "carcara: empty task on stdin" in capsys.readouterr().err
    assert RunStore(repo).list_runs() == []


# -- lock, started line, SIGTERM, status ----------------------------------------


def _lock(repo):
    return repo / ".carcara" / "active.json"


def _write_lock(repo, pid, run_id="other-run"):
    _lock(repo).parent.mkdir(parents=True, exist_ok=True)
    _lock(repo).write_text(json.dumps({"pid": pid, "run_id": run_id, "started": "x"}))


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_started_and_resumed_lines(repo, fake, capsys):
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    (run_id,) = RunStore(repo).list_runs()
    assert (
        capsys.readouterr().err.splitlines()[0]
        == f"carcara: run {run_id} started (profile balanced)"
    )
    fake({"implement": [IMPL], "test": [TEST_OK], "review": [REVIEW_OK]})
    assert run(repo, "--resume", run_id, "--yes") == 0
    assert (
        capsys.readouterr().err.splitlines()[0]
        == f"carcara: run {run_id} resumed (profile balanced)"
    )


def test_concurrent_run_is_busy_exit_6(repo, fake, capsys):
    _write_lock(repo, os.getpid())  # a live process (this one) under another run id
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "x", "--size", "S") == 6
    err = capsys.readouterr().err
    assert err == "carcara: another run is active: other-run (carcara status other-run)\n"
    assert RunStore(repo).list_runs() == []
    assert json.loads(_lock(repo).read_text())["run_id"] == "other-run"


def test_resume_while_busy_exit_6(repo, fake, capsys):
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    (run_id,) = RunStore(repo).list_runs()
    _write_lock(repo, os.getpid())
    capsys.readouterr()
    assert run(repo, "--resume", run_id, "--yes") == 6
    assert "another run is active: other-run" in capsys.readouterr().err
    state = _state(repo, run_id)
    assert state["status"] == "awaiting_approval"
    assert _lock(repo).exists()


@pytest.mark.parametrize("content", ["dead", "{not json", ""])
def test_stale_or_corrupt_lock_is_taken_over(repo, fake, content):
    if content == "dead":
        _write_lock(repo, _dead_pid())
    else:
        _lock(repo).parent.mkdir(parents=True, exist_ok=True)
        _lock(repo).write_text(content)
    assert RunStore(repo).active() is None
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "x", "--size", "S") == 0
    assert not _lock(repo).exists()


@pytest.mark.parametrize(
    "script,args,code",
    [
        ({"implement": [IMPL], "test": [TEST_OK]}, ["--size", "S"], 0),
        ({"explore": [EXPLORE], "plan": [PLAN]}, ["--size", "L"], 3),
        (
            {"implement": [IMPL, IMPL, IMPL], "test": [TEST_FAIL, TEST_FAIL, TEST_FAIL]},
            ["--size", "S", "--yes"],
            4,
        ),
        ({"explore": [EXPLORE], "plan": [PLAN]}, ["--size", "M", "--max-budget-usd", "0.5"], 5),
        ({"implement": []}, ["--size", "S"], 1),
    ],
)
def test_lock_released_on_exit(repo, fake, monkeypatch, script, args, code):
    fake(script, {"explore": 1.0})
    seen = []
    real_create = RunStore.create

    def spy(self, *a, **kw):
        created = real_create(self, *a, **kw)
        seen.append(self.lock_path.exists())
        return created

    monkeypatch.setattr(RunStore, "create", spy)
    assert run(repo, "x", *args) == code
    assert seen == [False]
    assert not _lock(repo).exists()


def test_lock_held_during_run_and_released_on_interrupt(repo, fake, monkeypatch, capsys):
    held = []

    def boom(self, plan):
        held.append(RunStore(repo).active())
        raise KeyboardInterrupt

    monkeypatch.setattr(NonInteractiveGate, "approve_plan", boom)
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 130
    (run_id,) = RunStore(repo).list_runs()
    assert held[0]["run_id"] == run_id and held[0]["pid"] == os.getpid()
    assert not _lock(repo).exists()


def test_sigterm_handler_raises_keyboard_interrupt():
    import signal

    before = signal.getsignal(signal.SIGTERM)
    with cli._sigterm_as_interrupt():
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    assert signal.getsignal(signal.SIGTERM) is before


SLOW_RUNNER = """
import asyncio, sys, time
from carcara import backend, cli
orig = backend.FakeBackend.run_stage
async def slow(self, request):
    if request.stage == "implement":
        SLEEP
    return await orig(self, request)
backend.FakeBackend.run_stage = slow
sys.exit(cli.main(sys.argv[1:]))
"""


# Blocking sleep: the signal lands in the coroutine frame; asyncio.sleep: it
# lands in the event loop's selector (like the real SDK backend awaiting I/O).
@pytest.mark.parametrize("sleep", ["time.sleep(60)", "await asyncio.sleep(60)"])
def test_sigterm_saves_state_and_exits_130(repo, fake, tmp_path, sleep):
    import signal

    script = tmp_path / "slow_runner.py"
    script.write_text(SLOW_RUNNER.replace("SLEEP", sleep))
    fake({"implement": [IMPL], "test": [TEST_OK]})
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(script), "run", "x", "--size", "S", "--cwd", str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    lines = []
    try:
        for line in proc.stderr:
            lines.append(line)
            if line.startswith("carcara: implement ("):
                break
        assert _lock(repo).exists()
        proc.send_signal(signal.SIGTERM)
        _, rest = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, lines + [rest]
    (run_id,) = RunStore(repo).list_runs()
    assert lines[0] == f"carcara: run {run_id} started (profile balanced)\n"
    assert "interrupted; state saved" in rest
    assert _state(repo, run_id)["status"] == "running"
    assert not _lock(repo).exists()


def status(repo, *args):
    return main(["status", *args, "--cwd", str(repo)])


def _status_json(repo, capsys, *args):
    capsys.readouterr()
    assert status(repo, "--json", *args) == 0
    return json.loads(capsys.readouterr().out)


STATUS_KEYS = {
    "run_id",
    "status",
    "size",
    "exit_code",
    "message",
    "report",
    "plan",
    "failing",
    "resume_cmd",
    "active",
    "cost_usd",
    "failed_attempts",
    "uncounted_stages",
    "gate",
    "unverified",
    "probe_results",
    "extent",
    "triage_range",
    "uncertainty_kind",
    "issue",
    "card_estimate",
    "urutau",
}


def test_status_no_runs(repo, capsys):
    assert status(repo) == 1
    assert capsys.readouterr().err == "carcara: no runs\n"
    assert status(repo, "nope") == 1
    assert "unknown run: nope" in capsys.readouterr().err


def test_status_awaiting_approval_json_and_plan(repo, fake, capsys):
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    (run_id,) = RunStore(repo).list_runs()
    data = _status_json(repo, capsys)
    assert set(data) == STATUS_KEYS
    assert data["run_id"] == run_id and data["status"] == "awaiting_approval"
    assert data["size"] == "L" and data["exit_code"] == 3 and data["active"] is False
    assert data["plan"] == PLAN and data["failing"] == []
    assert data["report"].startswith(f"carcara run {run_id}: awaiting_approval")
    assert data["resume_cmd"] == f"carcara run --resume {run_id} --cwd {repo} --yes"
    assert data["gate"] == {"trigger": "size", "paths": [], "stage": "plan"}
    assert data["unverified"] == [] and data["probe_results"] == {}

    assert status(repo, "--plan") == 0
    assert capsys.readouterr().out.startswith("Plan: do the thing\n")

    assert status(repo, run_id) == 0
    out = capsys.readouterr().out
    assert f"carcara run {run_id}: awaiting_approval" in out
    assert f"resume: carcara run --resume {run_id} --cwd {repo} --yes" in out


def test_status_needs_human_failing(repo, fake, capsys):
    fake({"implement": [IMPL, IMPL, IMPL], "test": [TEST_FAIL, TEST_FAIL, TEST_FAIL]})
    assert run(repo, "small fix", "--size", "S", "--yes") == 4
    (run_id,) = RunStore(repo).list_runs()
    data = _status_json(repo, capsys, run_id)
    assert data["exit_code"] == 4 and data["plan"] is None
    assert data["failing"] == [{"kind": "test", "name": "t1", "detail": "x"}]
    assert data["resume_cmd"] == f'carcara run --resume {run_id} --cwd {repo} --feedback "..."'
    assert status(repo, "--plan") == 1
    assert f"run {run_id} has no plan" in capsys.readouterr().err


def test_status_json_includes_uncounted(repo, fake, capsys):
    # No test entry: the stage errors before a result, so its cost is unknown.
    fake({"implement": [IMPL]}, {"implement": 0.25})
    assert run(repo, "small fix", "--size", "S", "--yes") == 1
    (run_id,) = RunStore(repo).list_runs()
    data = _status_json(repo, capsys)
    assert data["cost_usd"] == pytest.approx(0.25)
    assert data["failed_attempts"] == 1 and data["uncounted_stages"] == ["test"]
    assert "(+1 stage attempt uncounted)" in data["report"]

    assert run(repo, "--list") == 0
    assert "$0.25+?" in capsys.readouterr().out

    # Without report.md the text status still warns about the unknown cost.
    (RunStore(repo).load(run_id).dir / "report.md").unlink()
    assert status(repo) == 0
    out = capsys.readouterr().out
    assert "warning: cost unknown for 1 failed stage attempt(s): test" in out


def test_status_json_old_state_defaults(repo, capsys):
    run_ = RunStore(repo).create("t", "balanced", "sha", size="S")
    del run_.state["failed_attempts"]
    run_.save()
    data = _status_json(repo, capsys)
    assert data["cost_usd"] == 0.0
    assert data["failed_attempts"] == 0 and data["uncounted_stages"] == []


def test_status_verifiability_fields(repo, capsys):
    run_ = RunStore(repo).create("t", "balanced", "sha", size="S")
    gate = {"trigger": "verifiability", "paths": [".github/x.yml"], "stage": "post-implement"}
    extent = {
        "rule": "carcara/extent-1",
        "files_changed": 1,
        "areas": [".github"],
        "areas_truncated": False,
        "fix_rounds": 0,
    }
    items = [
        {"id": "U1", "kind": "external", "text": "pypi name free"},
        {"id": "U2", "kind": "normative", "text": "done", "resolved": True},
    ]
    run_.state.update(gate=gate, extent=extent, unverified=items)
    run_.save()
    data = _status_json(repo, capsys)
    assert data["gate"] == gate and data["extent"] == extent
    assert data["unverified"] == items and data["probe_results"] == {}

    assert status(repo) == 0
    out = capsys.readouterr().out
    assert "gate: verifiability (paths: .github/x.yml)\n" in out
    assert "extent: 1 files, areas .github, fix rounds 0 [carcara/extent-1]\n" in out
    assert "unverified: 1 open (external 1, normative 0, untested 0)\n" in out
    assert "  - U1 [external] pypi name free\n" in out and "U2" not in out


def test_status_review_findings_and_done(repo):
    store = RunStore(repo)
    run_ = store.create("t", "balanced", "sha", size="S")
    finding = {"severity": "major", "path": "a.py", "issue": "bug", "fix": "f"}
    nit = {**finding, "severity": "nit"}
    stages = [
        ("test", TEST_OK),
        ("review", {"verdict": "request_changes", "findings": [finding, nit]}),
    ]
    for key, out in stages:
        run_.record_stage(key, key, None, "m", out, 0.0, None, 1, None)
    assert cli._failing(run_.state) == [{"kind": "review", **finding}]
    assert cli._status_resume_cmd(run_.id, "done", False, ".") is None
    assert cli._status_resume_cmd(run_.id, "running", True, ".") is None
    assert cli._status_resume_cmd(run_.id, "running", False, ".") == (
        f"carcara run --resume {run_.id}"
    )


def test_status_default_prefers_active_then_latest(repo, capsys):
    store = RunStore(repo)
    first = store.create("one", "balanced", "sha")
    second = store.create("two", "balanced", "sha")
    older, latest = sorted([first.id, second.id])
    assert _status_json(repo, capsys)["run_id"] == latest

    _write_lock(repo, os.getpid(), older)
    data = _status_json(repo, capsys)
    assert data["run_id"] == older and data["active"] is True
    assert data["status"] == "running" and data["exit_code"] is None
    assert data["resume_cmd"] is None

    assert status(repo) == 0
    out = capsys.readouterr().out
    assert f"carcara run {older}: running" in out and f"pid {os.getpid()}" in out

    _write_lock(repo, _dead_pid(), older)  # stale lock: back to the latest run
    assert _status_json(repo, capsys)["run_id"] == latest
    data = _status_json(repo, capsys, older)
    assert data["active"] is False
    assert data["resume_cmd"] == f"carcara run --resume {older} --cwd {repo}"


PLAN_R1 = {**PLAN, "steps": [{"id": "step-r1", "files": ["c.py"], "change": "revised"}]}


def _deferred_l_run(repo, fake, capsys):
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    capsys.readouterr()
    return RunStore(repo).list_runs()[-1]


def test_cli_reject_feedback_replans_then_yes(repo, fake, capsys):
    run_id = _deferred_l_run(repo, fake, capsys)
    fake({"plan": [PLAN_R1]})
    assert run(repo, "--resume", run_id, "--reject", "--feedback", "split it") == 3
    capsys.readouterr()
    assert main(["status", run_id, "--json", "--cwd", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out)["plan"] == PLAN_R1
    fake({"implement": [IMPL], "test": [TEST_OK], "review": [REVIEW_OK]})
    assert run(repo, "--resume", run_id, "--yes") == 0
    keys = [s["key"] for s in RunStore(repo).load(run_id).state["stages"]]
    assert keys == ["explore", "architect", "architect:r1", "implement:step-r1", "test", "review"]


def test_cli_feedback_from_stdin(repo, fake, monkeypatch, capsys):
    run_id = _deferred_l_run(repo, fake, capsys)
    fake({"plan": [PLAN_R1]})
    monkeypatch.setattr("sys.stdin", io.StringIO("from\nstdin\n"))
    assert run(repo, "--resume", run_id, "--feedback", "-") == 3
    assert RunStore(repo).load(run_id).state["plan_feedback"] == ["from\nstdin"]


def test_cli_reject_alone_fails(repo, fake, capsys):
    run_id = _deferred_l_run(repo, fake, capsys)
    fake({})
    assert run(repo, "--resume", run_id, "--reject") == 1
    assert "plan rejected by user" in capsys.readouterr().out


def test_cli_guided_retry_and_accept_failures(repo, fake, capsys):
    fake({"implement": [IMPL] * 3, "test": [TEST_FAIL] * 3})
    assert run(repo, "fix", "--size", "S") == 4
    run_id = RunStore(repo).list_runs()[-1]
    # Guided retry still failing (fix loop exhausted again) -> needs_human.
    fake({"implement": [IMPL] * 3, "test": [TEST_FAIL] * 3})
    assert run(repo, "--resume", run_id, "--feedback", "try harder") == 4
    keys = [s["key"] for s in RunStore(repo).load(run_id).state["stages"]]
    assert "retry-1:guided-implement" in keys and "retry-1:fix-2:test" in keys
    fake({})
    assert run(repo, "--resume", run_id, "--accept-failures") == 0
    state = RunStore(repo).load(run_id).state
    assert (state["status"], state["accepted_failures"]) == ("done", True)


@pytest.mark.parametrize(
    "args",
    [
        ["task", "--reject"],
        ["task", "--feedback", "x"],
        ["task", "--accept-failures"],
        ["--resume", "R", "--reject", "--yes"],
        ["--resume", "R", "--accept-failures", "--feedback", "x"],
        ["--resume", "R", "--accept-failures", "--reject"],
        ["--resume", "R", "--feedback", "  "],
    ],
)
def test_cli_invalid_resume_flag_combos(repo, fake, capsys, args):
    fake({})
    assert run(repo, *args) == 1
    assert "carcara: " in capsys.readouterr().err
    assert not (repo / ".carcara").exists()


def test_cli_status_dependent_flag_errors(repo, fake, capsys):
    run_id = _deferred_l_run(repo, fake, capsys)
    fake({})
    assert run(repo, "--resume", run_id, "--accept-failures") == 1
    assert run(repo, "--resume", run_id, "--feedback", "x", "--yes") == 1
    assert RunStore(repo).load(run_id).state["status"] == "awaiting_approval"


def test_status_json_report_null_while_running(repo, capsys):
    run_ = RunStore(repo).create("t", "balanced", "sha")
    run_.write_report("stale report from a previous drive")
    data = _status_json(repo, capsys, run_.id)
    assert data["status"] == "running" and data["report"] is None


@pytest.mark.parametrize(
    "args",
    [["run", "x", "--ye"], ["status", "--js"], ["diff", "--sta"], ["routing", "on", "--proj", "."]],
)
def test_subcommand_flags_not_abbreviated(repo, args):
    with pytest.raises(SystemExit) as info:
        main([*args, "--cwd", str(repo)] if args[0] != "routing" else args)
    assert info.value.code == 2


def test_cli_rejects_non_toplevel_cwd(repo, fake, capsys):
    (repo / "sub").mkdir()
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert main(["run", "x", "--size", "S", "--cwd", str(repo / "sub")]) == 1
    assert capsys.readouterr().err == f"carcara: run from the repository root ({repo})\n"
    assert main(["diff", "--cwd", str(repo / "sub")]) == 1
    assert capsys.readouterr().err == f"carcara: run from the repository root ({repo})\n"
    assert main(["run", "--resume", "20990101-000000-abcd", "--cwd", str(repo / "sub")]) == 1
    assert capsys.readouterr().err == f"carcara: run from the repository root ({repo})\n"
    assert not (repo / "sub" / ".carcara").exists()


def test_diff_command(repo, fake, capsys):
    assert main(["diff", "--cwd", str(repo)]) == 1
    assert capsys.readouterr().err == "carcara: no runs\n"
    (repo / ".env").write_text("TRACKED_SECRET=1\n")
    subprocess.run(["git", "add", ".env"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "env"], cwd=repo, check=True)
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "x", "--size", "S") == 0
    (run_id,) = RunStore(repo).list_runs()
    out = capsys.readouterr().out
    assert f"carcara changes: carcara diff {run_id}" in out
    (repo / "a.py").write_text("x = 2\n")
    (repo / ".env").write_text("TRACKED_SECRET=2\n")
    assert main(["diff", "--cwd", str(repo)]) == 0
    diff = capsys.readouterr().out
    assert "+x = 2" in diff and "SECRET" not in diff and ".env" not in diff
    assert main(["diff", run_id, "--stat", "--cwd", str(repo)]) == 0
    stat = capsys.readouterr().out
    assert "a.py" in stat and "+x" not in stat and ".env" not in stat
    assert main(["diff", "nope", "--cwd", str(repo)]) == 1
    assert "unknown run: nope" in capsys.readouterr().err


def test_stale_lock_takeover_does_not_discard_fresh_lock(repo):
    from carcara.runstore import RunBusy

    store = RunStore(repo)
    _write_lock(repo, _dead_pid(), "stale-run")
    real_read = store._read_lock_text
    calls = []

    def interleaved():
        text = real_read()
        if not calls:
            # Another process takes over the stale lock right after our read.
            _write_lock(repo, os.getpid(), "fresh-run")
        calls.append(text)
        return text

    store._read_lock_text = interleaved
    with pytest.raises(RunBusy) as info:
        store.acquire_lock("mine")
    assert info.value.run_id == "fresh-run"
    assert json.loads(_lock(repo).read_text())["run_id"] == "fresh-run"
    assert list(store.base.glob(".active.json.*")) == []


def test_lock_with_mismatched_start_identity_is_stale(repo, monkeypatch):
    from carcara import runstore

    store = RunStore(repo)
    monkeypatch.setattr(runstore, "_proc_start", lambda pid: "this-process")
    store.acquire_lock("r1")
    assert json.loads(_lock(repo).read_text())["start"] == "this-process"
    assert store.active()["run_id"] == "r1"
    monkeypatch.setattr(runstore, "_proc_start", lambda pid: "other-process")
    assert store.active() is None  # same pid, different process: reused pid
    store.acquire_lock("r2")  # takes over the stale lock
    assert store.active()["run_id"] == "r2"
    monkeypatch.setattr(runstore, "_proc_start", lambda pid: None)  # unknown: pid-only
    assert store.active()["run_id"] == "r2"
    store.release_lock("r2")
    store.acquire_lock("r3")
    assert "start" not in json.loads(_lock(repo).read_text())
    store.release_lock("r3")


def test_proc_start_identity():
    from carcara.runstore import _proc_start

    if sys.platform.startswith("linux"):
        assert _proc_start(os.getpid()) == _proc_start(os.getpid()) is not None
        assert _proc_start(_dead_pid()) is None


def _events(repo, run_id):
    path = RunStore(repo).load(run_id).dir / "events.jsonl"
    return [json.loads(line)["event"] for line in path.read_text().splitlines()]


def test_unrestricted_bash_flag_warns_and_reaches_stages(repo, fake, monkeypatch, capsys):
    from carcara.orchestrator import Orchestrator

    seen = []
    real_init = Orchestrator.__init__

    def spy(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        seen.append(self.options.unrestricted_bash)

    monkeypatch.setattr(Orchestrator, "__init__", spy)
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "tiny", "--size", "S") == 0
    assert "--unrestricted-bash" not in capsys.readouterr().err
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "tiny", "--size", "S", "--unrestricted-bash") == 0
    assert "warning: --unrestricted-bash" in capsys.readouterr().err
    assert seen == [False, True]
    flagged = [r for r in RunStore(repo).list_runs() if "warning" in _events(repo, r)]
    assert len(flagged) == 1


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:octo/repo.git",
        "git@github.com:octo/repo",
        "https://github.com/octo/repo.git",
        "https://github.com/octo/repo",
    ],
)
def test_origin_repo_parses_github_remotes(repo, url):
    subprocess.run(["git", "remote", "add", "origin", url], cwd=repo, check=True)
    assert cli._origin_repo(str(repo)) == "octo/repo"


def test_origin_repo_unknown_remote(repo):
    with pytest.raises(cli.IssueError, match="pass --repo owner/name"):
        cli._origin_repo(str(repo))


@pytest.fixture
def no_token(tmp_path, monkeypatch):
    monkeypatch.delenv("URUTAU_MCP_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.mark.parametrize("no_urutau", [False, True])
def test_issue_builds_task_without_urutau(repo, fake, monkeypatch, no_token, no_urutau):
    calls = []

    def gh(repo_name, n):
        calls.append((repo_name, n))
        return "Fix the thing", "It is broken."

    monkeypatch.setattr(cli, "_gh_issue", gh)
    if no_urutau:
        monkeypatch.setenv("URUTAU_MCP_TOKEN", "tok-SECRET-123")
    fake({"implement": [IMPL], "test": [TEST_OK]})
    args = ["also add a test", "--issue", "7", "--repo", "octo/repo", "--size", "S"]
    assert run(repo, *args, *(["--no-urutau"] if no_urutau else [])) == 0
    assert calls == [("octo/repo", 7)]
    (run_id,) = RunStore(repo).list_runs()
    state = _state(repo, run_id)
    assert state["task"] == (
        "GitHub issue #7: Fix the thing\n\nIt is broken.\n\n"
        "Additional instructions:\nalso add a test"
    )
    assert state["issue"] == 7
    assert state["urutau"]["enabled"] is False and state["urutau"]["repo"] == "octo/repo"


def test_issue_flag_errors(repo, fake, capsys):
    assert run(repo, "x", "--repo", "octo/repo") == 1
    assert "--repo requires --issue" in capsys.readouterr().err
    assert run(repo, "--resume", "r1", "--issue", "3") == 1
    assert "--issue cannot be combined with --resume" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run(repo, "--issue", "0")


def test_dry_run_issue_shows_urutau_state(repo, monkeypatch, no_token, capsys):
    monkeypatch.setattr(cli, "_gh_issue", lambda *a: pytest.fail("gh called in dry-run"))
    assert run(repo, "--dry-run", "--issue", "5", "--repo", "octo/repo") == 0
    assert "issue #5 (octo/repo); Urutau reporting: off" in capsys.readouterr().out
