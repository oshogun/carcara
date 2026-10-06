"""Profile recorded by `carcara install` and picked up by `carcara run` (#16)."""

import io
import json
import os
import subprocess

import pytest

from carcara.cli import main
from carcara.installer import install
from carcara.profiles import read_installed_profile
from carcara.runstore import RunStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM = os.path.join(ROOT, "tests", "fixtures", "custom.env")
IMPL = {
    "changed": [{"path": "a.py", "summary": "s"}],
    "verified": "pytest",
    "notes": "",
    "blocked": False,
    "user_facing_change": False,
}
TEST_OK = {"passed": True, "commands": ["pytest"], "failures": []}
EXPLORE = {"summary": "found", "findings": [{"path": "a.py", "line": 1, "fact": "x"}]}
PLAN = {
    "goal": "do the thing",
    "steps": [{"id": "step-1", "files": ["a.py"], "change": "edit a"}],
    "tests": ["pytest"],
    "acceptance": ["works"],
    "risks": ["none"],
}
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
    git(path, "init", "-q")
    commit(path)
    return path


@pytest.fixture
def fake(tmp_path, monkeypatch):
    def use(script):
        path = tmp_path / "script.json"
        path.write_text(json.dumps({"script": script, "costs": {}}))
        monkeypatch.setenv("CARCARA_BACKEND", f"fake:{path}")

    return use


def git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True)


def commit(path):
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "c")


def installed(repo, profile, capsys):
    install(str(repo), profile)
    commit(repo)
    capsys.readouterr()


def run(repo, *args):
    return main(["run", *args, "--cwd", str(repo)])


def dry_run_header(capsys):
    return capsys.readouterr().out.splitlines()[0]


# --- install records the profile ---------------------------------------------


def test_install_records_profile(tmp_path, capsys):
    install(str(tmp_path), "quality")
    assert (tmp_path / ".carcara" / "profile").read_text() == "quality\n"
    assert (tmp_path / ".carcara" / ".gitignore").read_text() == "*\n"
    assert f"  create     {tmp_path}/.carcara/profile\n" in capsys.readouterr().out
    assert read_installed_profile(str(tmp_path)) == "quality"


def test_reinstall_updates_record_and_dry_run_writes_nothing(tmp_path, capsys):
    install(str(tmp_path), "quality")
    capsys.readouterr()
    install(str(tmp_path), "quality")
    assert "/.carcara/profile" not in capsys.readouterr().out  # unchanged: no action
    install(str(tmp_path), "economy", force=True)
    assert f"  overwrite  {tmp_path}/.carcara/profile\n" in capsys.readouterr().out
    assert (tmp_path / ".carcara" / "profile").read_text() == "economy\n"
    install(str(tmp_path), "quality", force=True, dry_run=True)
    assert f"  overwrite  {tmp_path}/.carcara/profile\n" in capsys.readouterr().out
    assert (tmp_path / ".carcara" / "profile").read_text() == "economy\n"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    install(str(fresh), "quality", dry_run=True)
    assert os.listdir(fresh) == []


def test_custom_profile_recorded_as_absolute_path(repo, tmp_path, monkeypatch, capsys):
    (tmp_path / "quality.env").write_bytes(open(CUSTOM, "rb").read())
    monkeypatch.chdir(tmp_path)
    install(str(repo), "quality.env")  # relative, named like a built-in
    recorded = (repo / ".carcara" / "profile").read_text()
    assert recorded == f"{tmp_path / 'quality.env'}\n"
    commit(repo)
    capsys.readouterr()
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    assert main(["run", "--dry-run", "--cwd", "."]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("carcara run --dry-run (profile quality, installed;")
    assert "implementer-model" in out


# --- run picks it up ---------------------------------------------------------


def test_dry_run_uses_installed_profile(repo, capsys):
    installed(repo, "quality", capsys)
    assert run(repo, "--dry-run") == 0
    assert dry_run_header(capsys) == (
        "carcara run --dry-run (profile quality, installed; no backend calls)"
    )


def test_explicit_profile_overrides_installed(repo, fake, capsys):
    installed(repo, "quality", capsys)
    assert run(repo, "--dry-run", "--profile", "economy") == 0
    assert dry_run_header(capsys) == (
        "carcara run --dry-run (profile economy, explicit; no backend calls)"
    )
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "small", "--size", "S", "--profile", "economy") == 0
    (run_id,) = RunStore(repo).list_runs()
    assert capsys.readouterr().err.splitlines()[0] == (
        f"carcara: run {run_id} started (profile economy)"
    )


def test_run_uses_installed_profile(repo, fake, capsys):
    installed(repo, "quality", capsys)
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "small", "--size", "S") == 0
    (run_id,) = RunStore(repo).list_runs()
    assert capsys.readouterr().err.splitlines()[0] == (
        f"carcara: run {run_id} started (profile quality)"
    )
    assert RunStore(repo).load(run_id).state["profile"] == "quality"


def test_no_record_defaults_to_balanced(repo, capsys):
    assert run(repo, "--dry-run") == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == (
        "carcara run --dry-run (profile balanced, default; no backend calls)"
    )
    assert captured.err == ""


@pytest.mark.parametrize("spec", ["nonexistent", "/no/such/profile.env"])
def test_unloadable_record_warns_and_falls_back(repo, fake, capsys, spec):
    (repo / ".carcara").mkdir()
    (repo / ".carcara" / ".gitignore").write_text("*\n")
    (repo / ".carcara" / "profile").write_text(f"{spec}\n")
    assert run(repo, "--dry-run") == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == (
        "carcara run --dry-run (profile balanced, default; no backend calls)"
    )
    assert f"carcara: warning: installed profile {spec!r} could not be loaded" in captured.err
    assert "Traceback" not in captured.err
    fake({"implement": [IMPL], "test": [TEST_OK]})
    assert run(repo, "small", "--size", "S") == 0
    (run_id,) = RunStore(repo).list_runs()
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("carcara: warning: installed profile")
    assert err[1] == f"carcara: run {run_id} started (profile balanced)"


def test_resume_keeps_stored_profile(repo, fake, capsys):
    installed(repo, "quality", capsys)
    fake({"explore": [EXPLORE], "plan": [PLAN]})
    assert run(repo, "big change", "--size", "L") == 3
    (run_id,) = RunStore(repo).list_runs()
    capsys.readouterr()
    (repo / ".carcara" / "profile").write_text("economy\n")
    fake({"implement": [IMPL], "test": [TEST_OK], "review": [REVIEW_OK]})
    assert run(repo, "--resume", run_id, "--yes") == 0
    assert capsys.readouterr().err.splitlines()[0] == (
        f"carcara: run {run_id} resumed (profile quality)"
    )
