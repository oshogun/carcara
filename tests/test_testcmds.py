import asyncio
import os
import sys
import time

import pytest

from carcara import testcmds
from carcara.testcmds import (
    CommandResult,
    discover_test_commands,
    excerpt,
    run_command,
    run_commands,
)


def _run(cmd, cwd, timeout_s=10.0):
    return asyncio.run(run_command(cmd, cwd, timeout_s))


def _py(code):
    """A shell command running ``code`` with this interpreter (sh and cmd.exe)."""
    return f'"{sys.executable}" -c "{code}"'


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shell and process groups")


def test_run_command_success(tmp_path):
    r = _run("exit 0", tmp_path)
    assert r.exit_code == 0
    assert not r.timed_out
    assert r.ok


def test_run_command_failure_captures_output(tmp_path):
    r = _run(_py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), tmp_path)
    assert r.exit_code == 3
    assert not r.ok
    assert "out" in r.output_tail
    assert "err" in r.output_tail


def test_run_command_runs_in_cwd_with_closed_stdin(tmp_path):
    (tmp_path / "marker").write_text("x")
    r = _run(
        _py("import os, sys; print(os.listdir()); print('stdin=' + repr(sys.stdin.read()))"),
        tmp_path,
    )
    assert r.exit_code == 0
    assert "marker" in r.output_tail
    assert "stdin=''" in r.output_tail


def test_run_command_timeout(tmp_path):
    start = time.monotonic()
    r = _run(_py("import time; time.sleep(30)"), tmp_path, timeout_s=0.5)
    assert time.monotonic() - start < 5
    assert r.timed_out
    assert r.exit_code is None
    assert not r.ok
    assert "timed out after 0.5s" in testcmds.failure_detail(r)


@posix_only
def test_run_command_timeout_kills_background_children(tmp_path):
    pid_file = tmp_path / "pgid"
    start = time.monotonic()
    r = _run(f"echo $$ > {pid_file}; sleep 30 & sleep 30", tmp_path, timeout_s=0.5)
    assert time.monotonic() - start < 5
    assert r.timed_out
    pgid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


@posix_only
def test_run_command_background_child_holding_pipe_times_out(tmp_path):
    # The shell exits at once, but a backgrounded child keeps stdout open.
    start = time.monotonic()
    r = _run("sleep 30 & exit 0", tmp_path, timeout_s=0.5)
    assert time.monotonic() - start < 5
    assert r.timed_out


def test_run_command_truncates_output(tmp_path):
    r = _run(_py("for i in range(1, 5001): print(i)"), tmp_path)
    lines = r.output_tail.splitlines()
    assert len(lines) <= testcmds.TAIL_LINES
    assert len(r.output_tail) <= testcmds.TAIL_CHARS
    assert lines[-1] == "5000"

    r = _run(_py("import sys; sys.stdout.write('x' * 200000)"), tmp_path)
    assert len(r.output_tail) == testcmds.TAIL_CHARS


def test_run_commands_runs_all_after_failure(tmp_path):
    results = asyncio.run(run_commands(["exit 1", "echo second", "exit 0"], tmp_path, 10.0))
    assert [r.exit_code for r in results] == [1, 0, 0]
    assert "second" in results[1].output_tail


@posix_only
def test_run_command_cancel_kills_group(tmp_path):
    pid_file = tmp_path / "pgid"

    async def go():
        task = asyncio.ensure_future(run_command(f"echo $$ > {pid_file}; sleep 30", tmp_path, 60.0))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    pgid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_excerpt_only_failures():
    ok = CommandResult("true", 0, False, 0.1, "fine")
    bad = CommandResult("pytest", 1, False, 2.0, "FAILED test_x")
    slow = CommandResult("sleep 9", None, True, 1.0, "", timeout_s=1.0)
    text = excerpt([ok, bad, slow])
    assert "$ true" not in text
    assert "$ pytest\nexit code 1\nFAILED test_x" in text
    assert "$ sleep 9\ntimed out after 1s" in text
    assert excerpt([ok]) == ""


def test_excerpt_is_bounded():
    big = CommandResult("x", 1, False, 1.0, "y" * 100000)
    assert len(excerpt([big] * 5)) <= testcmds.EXCERPT_CHARS + 20


def test_discover_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\n\n[tool.ruff]\nline-length = 88\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    assert discover_test_commands(tmp_path) == [
        "ruff check .",
        "ruff format --check .",
        "pytest",
    ]


def test_discover_prefers_project_venv(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff.lint]\nselect = []\n\n[tool.pytest.ini_options]\n"
    )
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pytest").write_text("")
    assert discover_test_commands(tmp_path) == [
        "ruff check .",
        "ruff format --check .",
        ".venv/bin/pytest",
    ]


def test_discover_prefers_project_windows_venv(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff.lint]\nselect = []\n\n[tool.pytest.ini_options]\n"
    )
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pytest.exe").write_text("")
    (scripts / "ruff.exe").write_text("")
    assert discover_test_commands(tmp_path) == [
        ".venv\\Scripts\\ruff check .",
        ".venv\\Scripts\\ruff format --check .",
        ".venv\\Scripts\\pytest",
    ]
    # A POSIX venv layout wins when both exist.
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir()
    (bin_dir / "pytest").write_text("")
    assert discover_test_commands(tmp_path)[-1] == ".venv/bin/pytest"


def test_discover_regex_fallback_patterns():
    # Used when tomllib is unavailable (Python 3.10).
    assert testcmds._RUFF_RE.search("[project]\n[tool.ruff]\n")
    assert testcmds._RUFF_RE.search("[tool.ruff.lint]\n")
    assert not testcmds._RUFF_RE.search("[tool.ruffle]\n")
    assert testcmds._PYTEST_RE.search("[tool.pytest.ini_options]\n")
    assert not testcmds._PYTEST_RE.search("# [tool.pytest.ini_options]\n")


def test_discover_pytest_ini_and_tests_dir(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert discover_test_commands(tmp_path) == ["pytest"]
    other = tmp_path / "other"
    (other / "tests").mkdir(parents=True)
    assert discover_test_commands(other) == ["pytest"]


def test_discover_nothing(tmp_path):
    assert discover_test_commands(tmp_path) == []
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n')
    assert discover_test_commands(tmp_path) == []
