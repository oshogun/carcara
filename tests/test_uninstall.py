"""carcara uninstall: install -> uninstall round trips, user content kept."""

import json
import os
import shutil
from pathlib import Path

import pytest

from carcara import installer, runstore
from carcara.cli import main
from carcara.installer import INSTALL_MANIFEST_REL, hook_group

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = FIXTURES / "golden"

CASES = {
    "economy": "economy",
    "balanced": "balanced",
    "quality": "quality",
    "custom": str(FIXTURES / "custom.env"),
    "claude-md-append": "balanced",
    "claude-md-update": "balanced",
}

AGENT = Path(".claude") / "agents" / "reviewer.md"
SKILL = Path(".claude") / "skills" / "carcara" / "SKILL.md"


def tree(base: Path) -> dict[str, bytes]:
    return {
        p.relative_to(base).as_posix(): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


def dirs(base: Path) -> list[str]:
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_dir())


def without_block(data: bytes) -> bytes:
    lines = data.split(b"\n")
    begin = lines.index(b"<!-- carcara:begin -->")
    end = lines.index(b"<!-- carcara:end -->")
    return b"\n".join(lines[:begin] + lines[end + 1 :])


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    return d


def write_settings(d: Path, data: dict) -> bytes:
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(data, indent=2) + "\n").encode()
    (d / ".claude" / "settings.json").write_bytes(raw)
    return raw


@pytest.mark.parametrize("case", sorted(CASES))
def test_round_trip_golden(tmp_path, case, capsys):
    target = tmp_path / "proj"
    target.mkdir()
    gold = GOLDEN / case
    if (gold / "CLAUDE.md.input").exists():
        shutil.copyfile(gold / "CLAUDE.md.input", target / "CLAUDE.md")
    before = tree(target)
    assert main(["-p", CASES[case], str(target)]) == 0
    assert main(["uninstall", str(target)]) == 0
    if case == "claude-md-update":
        # The stale block was carcara's too: only the user content is left.
        before["CLAUDE.md"] = without_block(before["CLAUDE.md"])
    assert tree(target) == before
    assert dirs(target) == []


def test_round_trip_existing_settings(proj):
    user = {
        "permissions": {"allow": ["Bash(make:*)", "Read"], "deny": ["Read(./private/**)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-me"}]}]},
        "model": "haiku",
        "env": {"FOO": "1"},
    }
    raw = write_settings(proj, user)
    (proj / "CLAUDE.md").write_bytes(b"mine")
    assert main(["d"]) == 0
    data = json.loads((proj / ".claude" / "settings.json").read_bytes())
    assert data["permissions"]["allow"].count("Read") == 1
    assert main(["uninstall", "d"]) == 0
    # "Read" equals a template entry but was the user's: kept.
    assert tree(proj) == {".claude/settings.json": raw, "CLAUDE.md": b"mine"}


def test_round_trip_empty_preexisting_lists(proj):
    user = {"permissions": {"allow": [], "deny": []}, "hooks": {"PreToolUse": []}}
    raw = write_settings(proj, user)
    assert main(["d"]) == 0
    assert main(["uninstall", "d"]) == 0
    assert json.loads((proj / ".claude" / "settings.json").read_bytes()) == user
    assert tree(proj) == {".claude/settings.json": raw}


def test_round_trip_empty_preexisting_objects(proj):
    raw = write_settings(proj, {"permissions": {}, "hooks": {}})
    assert main(["d"]) == 0
    assert main(["uninstall", "d"]) == 0
    assert tree(proj) == {".claude/settings.json": raw}


def test_round_trip_absent_keys_still_dropped(proj):
    write_settings(proj, {"env": {"FOO": "1"}})
    assert main(["d"]) == 0
    assert main(["uninstall", "d"]) == 0
    assert json.loads((proj / ".claude" / "settings.json").read_text()) == {"env": {"FOO": "1"}}


def test_reinstall_preserves_created_keys(proj):
    user = {"permissions": {"deny": []}, "env": {"FOO": "1"}}
    write_settings(proj, user)
    assert main(["d"]) == 0
    assert main(["-p", "quality", "--strict-policy", "d"]) == 0
    manifest = json.loads((proj / INSTALL_MANIFEST_REL).read_text())
    assert manifest["settings_created_keys"] == {
        "permissions": False,
        "permission_keys": ["allow"],
        "hooks": True,
        "hook_events": ["PreToolUse", "UserPromptSubmit"],
    }
    assert main(["uninstall", "d"]) == 0
    assert json.loads((proj / ".claude" / "settings.json").read_text()) == user


def test_old_manifest_without_created_keys(proj):
    write_settings(proj, {"permissions": {"allow": []}, "env": {}})
    assert main(["d"]) == 0
    path = proj / INSTALL_MANIFEST_REL
    manifest = json.loads(path.read_text())
    del manifest["settings_created_keys"]
    path.write_text(json.dumps(manifest))
    assert main(["uninstall", "d"]) == 0
    # Old behaviour: whatever uninstall empties is dropped.
    assert json.loads((proj / ".claude" / "settings.json").read_text()) == {"env": {}}


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        {"permissions": True},
        {"permissions": 1, "hooks": True, "permission_keys": [], "hook_events": []},
        {"permissions": True, "hooks": True, "permission_keys": [1], "hook_events": []},
    ],
)
def test_load_manifest_rejects_malformed_created_keys(proj, capsys, bad):
    assert main(["d"]) == 0
    path = proj / INSTALL_MANIFEST_REL
    manifest = json.loads(path.read_text())
    manifest["settings_created_keys"] = bad
    path.write_text(json.dumps(manifest))
    before = tree(proj)
    capsys.readouterr()
    assert main(["uninstall", "d"]) == 1
    assert "install.json: unexpected content" in capsys.readouterr().err
    assert tree(proj) == before


def test_round_trip_reinstall(proj):
    (proj / "CLAUDE.md").write_text("# Mine\n")
    before = tree(proj)
    assert main(["d"]) == 0
    assert main(["-p", "quality", "--strict-policy", "d"]) == 0
    assert main(["-f", "-p", "economy", "d"]) == 0
    assert main(["uninstall", "d"]) == 0
    assert tree(proj) == before
    assert dirs(proj) == []


def test_user_edited_agent_kept(proj, capsys):
    assert main(["d"]) == 0
    (proj / AGENT).write_text("custom\n")
    capsys.readouterr()
    assert main(["uninstall", "d"]) == 0
    out = capsys.readouterr().out
    assert "  keep       d/.claude/agents/reviewer.md (modified; use --force to remove)\n" in out
    assert "  remove     d/.claude/agents/architect.md\n" in out
    assert tree(proj) == {".claude/agents/reviewer.md": b"custom\n"}
    assert main(["uninstall", "-f", "d"]) == 0
    assert "  remove     d/.claude/agents/reviewer.md\n" in capsys.readouterr().out
    assert list(proj.iterdir()) == []


def test_agent_from_other_profile_is_carcaras(proj):
    # Installed with one profile, the record says another: still recognised.
    assert main(["-p", "quality", "d"]) == 0
    (proj / ".carcara" / "profile").write_text("economy\n")
    assert main(["uninstall", "d"]) == 0
    assert list(proj.iterdir()) == []


def test_user_skill_not_removed(proj, capsys):
    skill = proj / SKILL
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: carcara\ndescription: mine\n---\nmy own skill\n")
    assert main(["--no-routing", "d"]) == 0
    assert main(["uninstall", "-f", "d"]) == 0
    assert "  keep       d/.claude/skills/carcara/SKILL.md (not installed by carcara)\n" in (
        capsys.readouterr().out
    )
    assert tree(proj) == {".claude/skills/carcara/SKILL.md": skill.read_bytes()}


def test_user_hook_and_model_change_kept(proj):
    assert main(["d"]) == 0
    path = proj / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    assert data["model"] == "sonnet"
    data["model"] = "opus"
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard"}]}
    data["hooks"]["PreToolUse"].append(mine)
    data["permissions"]["allow"].append("Bash(make:*)")
    path.write_text(json.dumps(data))
    assert main(["uninstall", "d"]) == 0
    assert json.loads(path.read_text()) == {
        "model": "opus",
        "permissions": {"allow": ["Bash(make:*)"]},
        "hooks": {"PreToolUse": [mine]},
    }
    assert sorted(os.listdir(proj / ".claude")) == ["settings.json"]


def test_user_content_in_created_claude_md_kept(proj):
    assert main(["d"]) == 0
    with open(proj / "CLAUDE.md", "a") as fh:
        fh.write("my notes\n")
    assert main(["uninstall", "d"]) == 0
    assert (proj / "CLAUDE.md").read_bytes() == b"my notes\n"


def test_runs_kept_without_purge(proj, capsys):
    assert main(["d"]) == 0
    run = proj / ".carcara" / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "state.json").write_text("{}")
    capsys.readouterr()
    assert main(["uninstall", "d"]) == 0
    assert "  keep       d/.carcara/runs (run history; use --purge to delete)\n" in (
        capsys.readouterr().out
    )
    assert tree(proj) == {".carcara/runs/r1/state.json": b"{}"}


def test_purge_removes_runs(proj, capsys):
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs" / "r1").mkdir(parents=True)
    assert main(["uninstall", "--purge", "d"]) == 0
    assert "  purge      d/.carcara/runs\n" in capsys.readouterr().out
    assert list(proj.iterdir()) == []


def test_purge_refuses_symlinked_runs(proj, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "r1").mkdir(parents=True)
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs").symlink_to(elsewhere)
    before = tree(proj)
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 1
    assert capsys.readouterr().err.startswith("carcara: refusing to purge d/.carcara/runs")
    assert tree(proj) == before
    assert (elsewhere / "r1").is_dir()


def write_lock(proj: Path, pid: int, start: str | None) -> None:
    lock = {"pid": pid, "run_id": "r1", "started": "2026-01-01T00:00:00+0000"}
    if start is not None:
        lock["start"] = start
    (proj / ".carcara" / "active.json").write_text(json.dumps(lock))


def test_purge_refuses_live_active_run(proj, capsys):
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs" / "r1").mkdir(parents=True)
    (proj / ".carcara" / "runs" / "r1" / "state.json").write_text("{}")
    write_lock(proj, os.getpid(), runstore._proc_start(os.getpid()))
    before, before_dirs = tree(proj), dirs(proj)
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("carcara: refusing to purge: run r1 is active")
    assert tree(proj) == before and dirs(proj) == before_dirs


@pytest.mark.parametrize("lock", ["dead", "reused", "corrupt"])
def test_purge_removes_stale_active_and_leftovers(proj, capsys, lock):
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs" / "r1").mkdir(parents=True)
    if lock == "dead":
        write_lock(proj, 2**22 + 12345, None)
    elif lock == "reused":
        if runstore._proc_start(os.getpid()) is None:
            pytest.skip("no process start identity on this platform")
        write_lock(proj, os.getpid(), "not-this-process")
    else:
        (proj / ".carcara" / "active.json").write_text("{not json")
    (proj / ".carcara" / ".active.json.123.ab.tmp").write_text("x")
    (proj / ".carcara" / ".active.json.123.ab.stale").write_text("x")
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 0
    out = capsys.readouterr().out
    assert "  purge      d/.carcara/active.json\n" in out
    assert "  purge      d/.carcara/.active.json.123.ab.stale\n" in out
    assert list(proj.iterdir()) == []


def test_purge_without_runs_dir_still_removes_stale_active(proj):
    assert main(["d"]) == 0
    (proj / ".carcara" / "active.json").write_text("{}")
    assert main(["uninstall", "--purge", "d"]) == 0
    assert list(proj.iterdir()) == []


def test_uninstall_without_purge_keeps_active_json(proj):
    assert main(["d"]) == 0
    (proj / ".carcara" / "active.json").write_text("{}")
    assert main(["uninstall", "d"]) == 0
    assert tree(proj) == {".carcara/active.json": b"{}"}


def test_dry_run_purge_lists_active_leftovers(proj, capsys):
    assert main(["d"]) == 0
    (proj / ".carcara" / "active.json").write_text("{}")
    (proj / ".carcara" / ".active.json.1.a.tmp").write_text("")
    before, before_dirs = tree(proj), dirs(proj)
    capsys.readouterr()
    assert main(["uninstall", "-n", "--purge", "d"]) == 0
    out = capsys.readouterr().out
    assert "  purge      d/.carcara/active.json\n" in out
    assert "  purge      d/.carcara/.active.json.1.a.tmp\n" in out
    assert tree(proj) == before and dirs(proj) == before_dirs


def test_uninstall_refuses_directory_at_carcara_file(proj, capsys):
    (proj / "CLAUDE.md").write_text("# Mine\n")
    assert main(["d"]) == 0
    (proj / ".carcara" / "profile").unlink()
    (proj / ".carcara" / "profile").mkdir()
    (proj / ".carcara" / "profile" / "notes").write_text("mine")
    before, before_dirs = tree(proj), dirs(proj)
    capsys.readouterr()
    assert main(["uninstall", "d"]) == 1
    assert capsys.readouterr().err == (
        "carcara: d/.carcara/profile is a directory, not a carcara file; refusing to uninstall\n"
    )
    assert tree(proj) == before and dirs(proj) == before_dirs


LOCK_NAMES = ["active.json", ".active.json.123.ab.tmp", ".active.json.123.ab.stale"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("name", LOCK_NAMES)
def test_purge_refuses_directory_at_lock_path(proj, capsys, name, dry_run):
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs" / "r1").mkdir(parents=True)
    (proj / ".carcara" / name).mkdir()
    (proj / ".carcara" / name / "notes").write_text("mine")
    before, before_dirs = tree(proj), dirs(proj)
    capsys.readouterr()
    assert main(["uninstall", *(["-n"] if dry_run else []), "--purge", "d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"carcara: d/.carcara/{name} is a directory, not a carcara lock file; "
        "refusing to uninstall\n"
    )
    assert tree(proj) == before and dirs(proj) == before_dirs


def test_purge_holds_lock_against_starting_run(proj, capsys, monkeypatch):
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs" / "r1").mkdir(parents=True)
    real_rmtree = shutil.rmtree
    busy = []

    def rmtree(path, *args, **kwargs):
        if Path(path).name == "runs":
            before = sorted(os.listdir(path))
            # Orchestrator.run's order: lock first, then create the run dir.
            try:
                runstore.RunStore(proj).acquire_lock("r2")
            except runstore.RunBusy as exc:
                busy.append(exc.run_id)
            else:
                busy.append(None)
            assert sorted(os.listdir(path)) == before
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(installer.shutil, "rmtree", rmtree)
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 0
    assert busy == [installer.PURGE_LOCK_ID]
    out = capsys.readouterr().out
    assert "  purge      d/.carcara/runs\n" in out
    assert "active.json" not in out
    # Only r2's own acquire_lock (_ensure_root) recreated the .gitignore.
    assert tree(proj) == {".carcara/.gitignore": b"*\n"}
    assert dirs(proj) == [".carcara"]


def test_purge_refuses_when_lock_taken_after_check(proj, capsys, monkeypatch):
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs" / "r1").mkdir(parents=True)
    write_lock(proj, os.getpid(), runstore._proc_start(os.getpid()))
    before, before_dirs = tree(proj), dirs(proj)
    # The run starts between the read-only check and acquiring the lock.
    monkeypatch.setattr(installer.runstore, "live_lock_holder", lambda path: None)
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("carcara: refusing to purge: run r1 is active")
    assert tree(proj) == before and dirs(proj) == before_dirs


def test_purge_lock_race_without_runs_dir_leaves_nothing_behind(proj, monkeypatch):
    assert main(["d"]) == 0
    write_lock(proj, os.getpid(), runstore._proc_start(os.getpid()))
    before, before_dirs = tree(proj), dirs(proj)
    monkeypatch.setattr(installer.runstore, "live_lock_holder", lambda path: None)
    assert main(["uninstall", "--purge", "d"]) == 1
    assert tree(proj) == before and dirs(proj) == before_dirs


def test_purge_refuses_symlinked_carcara_dir(proj, tmp_path, capsys):
    assert main(["d"]) == 0
    elsewhere = tmp_path / "elsewhere"
    (proj / ".carcara").rename(elsewhere)
    (elsewhere / "runs" / "r1").mkdir(parents=True)
    (proj / ".carcara").symlink_to(elsewhere)
    before, before_dirs = tree(proj), dirs(proj)
    outside = tree(elsewhere)
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("carcara: refusing to purge d/.carcara: not a directory")
    assert tree(proj) == before and dirs(proj) == before_dirs
    assert tree(elsewhere) == outside and (elsewhere / "runs" / "r1").is_dir()


def test_purge_rechecks_runs_created_before_lock(proj, tmp_path, capsys, monkeypatch):
    assert main(["d"]) == 0
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "r1").mkdir(parents=True)
    real_acquire = installer._acquire_purge_lock

    def acquire(target):
        # runs/ shows up (as a symlink) between the checks and taking the lock.
        (proj / ".carcara" / "runs").symlink_to(elsewhere)
        return real_acquire(target)

    monkeypatch.setattr(installer, "_acquire_purge_lock", acquire)
    before = tree(proj)
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("carcara: refusing to purge d/.carcara/runs")
    (proj / ".carcara" / "runs").unlink()
    assert tree(proj) == before
    assert (elsewhere / "r1").is_dir()


def test_purge_removes_dangling_symlink_at_active(proj, capsys):
    assert main(["d"]) == 0
    (proj / ".carcara" / "active.json").symlink_to(proj / "nowhere")
    capsys.readouterr()
    assert main(["uninstall", "--purge", "d"]) == 0
    assert "  purge      d/.carcara/active.json\n" in capsys.readouterr().out
    assert list(proj.iterdir()) == []


def test_dry_run_writes_nothing(proj, capsys):
    (proj / "CLAUDE.md").write_text("# Mine\n")
    assert main(["d"]) == 0
    (proj / ".carcara" / "runs").mkdir()
    before, before_dirs = tree(proj), dirs(proj)
    capsys.readouterr()
    assert main(["uninstall", "-n", "--purge", "d"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("uninstalling from d (dry run)")
    for line in (
        "  remove     d/.claude/agents/architect.md\n",
        "  remove     d/.claude/settings.json\n",
        "  strip      d/CLAUDE.md\n",
        "  remove     d/.carcara/install.json\n",
        "  purge      d/.carcara/runs\n",
    ):
        assert line in out
    assert tree(proj) == before and dirs(proj) == before_dirs


@pytest.mark.parametrize("sub", ["", ".claude", "cfg"])
def test_refuses_home_and_claude_config_dir(tmp_path, monkeypatch, capsys, sub):
    home = tmp_path / "home"
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / ".claude" / "agents" / "reviewer.md").write_text("x\n")
    (home / "cfg").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "cfg"))
    before = tree(home)
    assert main(["uninstall", "-f", str(home / sub if sub else home)]) == 1
    assert capsys.readouterr().err.startswith(
        "carcara: refusing to uninstall from your home directory"
    )
    assert tree(home) == before


def test_unbalanced_markers_error(proj, capsys):
    assert main(["d"]) == 0
    (proj / "CLAUDE.md").write_bytes(b"<!-- carcara:begin -->\nx\n")
    before = tree(proj)
    capsys.readouterr()
    assert main(["uninstall", "d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err == "carcara: d/CLAUDE.md has unbalanced carcara markers; fix them manually\n"
    )
    assert tree(proj) == before


def test_no_manifest_fallback(proj, capsys):
    user = {"permissions": {"allow": ["Bash(make:*)"]}, "model": "haiku"}
    write_settings(proj, user)
    (proj / "CLAUDE.md").write_text("# Mine\n")
    assert main(["d"]) == 0
    (proj / INSTALL_MANIFEST_REL).unlink()
    capsys.readouterr()
    assert main(["uninstall", "d"]) == 0
    out = capsys.readouterr().out
    assert "warning: no install manifest" in out
    data = json.loads((proj / ".claude" / "settings.json").read_text())
    assert data == user
    assert (proj / "CLAUDE.md").read_text() == "# Mine\n"
    assert not (proj / ".carcara").exists()


def test_reinstall_over_pre_manifest_install_keeps_fallback(proj, capsys):
    assert main(["d"]) == 0
    (proj / INSTALL_MANIFEST_REL).unlink()
    assert main(["d"]) == 0
    assert not (proj / INSTALL_MANIFEST_REL).exists()
    assert main(["uninstall", "d"]) == 0
    # Without a manifest 'model' can't be told apart from the user's: kept.
    assert tree(proj) == {".claude/settings.json": b'{\n  "model": "sonnet"\n}\n'}


def test_idempotent_and_nothing_installed(proj, tmp_path, capsys):
    assert main(["d"]) == 0
    assert main(["uninstall", "d"]) == 0
    capsys.readouterr()
    assert main(["uninstall", "d"]) == 0
    assert capsys.readouterr().out.endswith("nothing to uninstall\n")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "CLAUDE.md").write_text("mine\n")
    write_settings(fresh, {"hooks": {}, "permissions": {"allow": []}})
    before = tree(fresh)
    assert main(["uninstall", "fresh"]) == 0
    assert capsys.readouterr().out.endswith("nothing to uninstall\n")
    assert tree(fresh) == before


def test_settings_hooks_only_strips_carcara(proj):
    raw = write_settings(proj, {"hooks": {"Stop": [hook_group("prompt-context")]}})
    assert main(["d"]) == 0
    assert main(["uninstall", "d"]) == 0
    # The carcara group was the only thing in the user's event: event dropped,
    # but the file existed before, so it stays.
    assert (proj / ".claude" / "settings.json").read_bytes() != raw
    assert json.loads((proj / ".claude" / "settings.json").read_text()) == {}


def test_uninstall_help_and_bad_args(capsys):
    assert main(["uninstall", "--help"]) == 0
    assert "Usage: carcara uninstall [options] [target-dir]" in capsys.readouterr().out
    assert main(["uninstall", "--bogus"]) == 1
    assert capsys.readouterr().err == "carcara: unknown option: --bogus (see --help)\n"
    assert main(["uninstall", "a", "b"]) == 1
    assert main(["uninstall", "missing"]) == 1
    assert "target directory does not exist" in capsys.readouterr().err
