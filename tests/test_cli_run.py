import io
import json
import os
import subprocess

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
REVIEW_OK = {"verdict": "approve", "findings": []}


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
