import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from carcara import __version__
from carcara.cli import main
from carcara.installer import install

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


def tree(base: Path) -> dict[str, bytes]:
    return {
        p.relative_to(base).as_posix(): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    # Empty argv installs into cwd: never let that be the repo root.
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("case", sorted(CASES))
def test_matches_golden(tmp_path, case, capsys):
    target = tmp_path / "proj"
    target.mkdir()
    gold = GOLDEN / case
    if (gold / "CLAUDE.md.input").exists():
        shutil.copyfile(gold / "CLAUDE.md.input", target / "CLAUDE.md")
    assert main(["-p", CASES[case], str(target)]) == 0
    assert tree(target / ".claude") == tree(gold / "dot-claude")
    assert (target / "CLAUDE.md").read_bytes() == (gold / "CLAUDE.md.golden").read_bytes()
    assert sorted(os.listdir(target)) == [".carcara", ".claude", "CLAUDE.md"]


def test_install_subcommand_equals_flag_only(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert main(["install", "-p", "quality", str(a)]) == 0
    assert main(["-p", "quality", str(b)]) == 0
    assert tree(a) == tree(b)


def test_default_target_is_cwd(tmp_path, capsys):
    assert main([]) == 0
    assert (tmp_path / ".claude" / "agents" / "explorer.md").is_file()
    out = capsys.readouterr().out
    assert "  create     ./.claude/agents/architect.md\n" in out
    assert out.endswith(
        "Next: start Claude Code in . and just ask for a change \u2014 carcara routes it. "
        "(`carcara routing off` to disable.)\n"
    )


def test_output_format(tmp_path, capsys):
    (tmp_path / "d").mkdir()
    install("d", "economy")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"carcara {__version__}: installing profile 'economy' into d"
    assert out[1] == "  create     d/.claude/agents/architect.md"
    assert out[-6:-2] == [
        "  create     d/CLAUDE.md",
        "  create     d/.carcara/.gitignore",
        "  create     d/.carcara/profile",
        "  create     d/.carcara/install.json",
    ]
    assert out[-2] == "done: 13 file(s) written, 0 skipped."


def test_skip_and_force(tmp_path, capsys):
    d = tmp_path / "d"
    (d / ".claude" / "agents").mkdir(parents=True)
    (d / ".claude" / "agents" / "reviewer.md").write_text("custom\n")
    assert main(["d"]) == 0
    out = capsys.readouterr().out
    assert "  skip       d/.claude/agents/reviewer.md (exists; use --force to overwrite)\n" in out
    assert "done: 12 file(s) written, 1 skipped.\n" in out
    assert "re-run with --force to apply profile 'balanced' to them.\n" in out
    assert (d / ".claude" / "agents" / "reviewer.md").read_text() == "custom\n"
    assert main(["--force", "d"]) == 0
    out = capsys.readouterr().out
    assert "  overwrite  d/.claude/agents/reviewer.md\n" in out
    assert "  update     d/CLAUDE.md\n" in out
    assert "name: reviewer\n" in (d / ".claude" / "agents" / "reviewer.md").read_text()


def test_dry_run_writes_nothing(tmp_path, capsys):
    d = tmp_path / "d"
    d.mkdir()
    assert main(["-n", "d"]) == 0
    assert list(d.iterdir()) == []
    assert capsys.readouterr().out.splitlines()[0].endswith(" (dry run)")
    (d / "CLAUDE.md").write_bytes(b"mine")
    assert main(["--dry-run", "d"]) == 0
    assert "  append     d/CLAUDE.md\n" in capsys.readouterr().out
    assert sorted(os.listdir(d)) == ["CLAUDE.md"]
    assert (d / "CLAUDE.md").read_bytes() == b"mine"


def test_append_without_trailing_newline(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "CLAUDE.md").write_bytes(b"mine")
    assert main(["d"]) == 0
    assert (d / "CLAUDE.md").read_bytes().startswith(b"mine\n<!-- carcara:begin -->\n")


def test_idempotent_and_preserves_user_content(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "CLAUDE.md").write_text("# My project\n\nKeep this line.\n")
    assert main(["d"]) == 0
    first = (d / "CLAUDE.md").read_bytes()
    assert main(["-p", "quality", "d"]) == 0
    assert b"profile: quality" in (d / "CLAUDE.md").read_bytes()
    assert main(["d"]) == 0
    assert (d / "CLAUDE.md").read_bytes() == first
    with open(d / "CLAUDE.md", "a") as fh:
        fh.write("after\n")
    assert main(["d"]) == 0
    assert (d / "CLAUDE.md").read_bytes() == first + b"after\n"


@pytest.mark.parametrize(
    "content",
    [
        b"<!-- carcara:begin -->\nx\n",
        b"a\n<!-- carcara:end -->\n<!-- carcara:begin -->\nb\n",
        b"<!-- carcara:begin -->\n<!-- carcara:end -->\n" * 2,
        b"<!-- carcara:end -->\n",
    ],
)
def test_unbalanced_markers_fail_before_any_write(tmp_path, capsys, content):
    d = tmp_path / "d"
    d.mkdir()
    (d / "CLAUDE.md").write_bytes(content)
    assert main(["d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err == "carcara: d/CLAUDE.md has unbalanced carcara markers; fix them manually\n"
    )
    assert sorted(os.listdir(d)) == ["CLAUDE.md"]
    assert (d / "CLAUDE.md").read_bytes() == content


@pytest.mark.parametrize(
    "argv,err",
    [
        (["-p", "nope", "d"], "unknown profile: nope (try --list-profiles)"),
        (["-p", "../x", "d"], "unknown profile: ../x"),
        (["missing"], "target directory does not exist: missing"),
        (["--bogus", "d"], "unknown option: --bogus (see --help)"),
        (["-"], "unknown option: - (see --help)"),
        (["d", "d"], "only one target directory may be given"),
        (["d", "--", "d"], "only one target directory may be given"),
        (["--", "d", "d"], "only one target directory may be given"),
        (["-p"], "-p requires an argument"),
        (["--profile"], "--profile requires an argument"),
    ],
)
def test_invalid_input(tmp_path, capsys, argv, err):
    (tmp_path / "d").mkdir()
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"carcara: {err}\n"
    assert list((tmp_path / "d").iterdir()) == []


def test_profile_equals_form_and_double_dash(tmp_path):
    (tmp_path / "-d").mkdir()
    assert main(["--profile=quality", "--", "-d"]) == 0
    assert "model: opus" in (tmp_path / "-d" / ".claude" / "agents" / "implementer.md").read_text()


@pytest.mark.parametrize("argv", [["-l"], ["--list-profiles"], ["profiles"], ["install", "-l"]])
def test_list_profiles_aliases(capsys, argv):
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert [n for n in out.splitlines() if not n.startswith(" ")] == [
        "balanced",
        "economy",
        "quality",
    ]


def test_install_help_and_version(capsys):
    assert main(["install", "--help"]) == 0
    assert "Usage: carcara [options] [target-dir]" in capsys.readouterr().out
    assert main(["install", "-V"]) == 0
    assert capsys.readouterr().out == f"carcara {__version__}\n"


# --- install snapshot (originally parity with the 0.1.0 bash installer) -----
# tests/fixtures/install_snapshot.json holds the exit code, stdout, stderr and
# resulting tree (sha256 per file) for each scenario (see golden/README.md).

PARITY = json.loads((FIXTURES / "install_snapshot.json").read_text())


def _existing(proj):
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / ".claude" / "agents" / "reviewer.md").write_text("custom\n")
    (proj / "CLAUDE.md").write_bytes((GOLDEN / "claude-md-update" / "CLAUDE.md.input").read_bytes())


def _unbalanced(proj):
    (proj / "CLAUDE.md").write_bytes(b"<!-- carcara:begin -->\nx\n")


SETUPS = {None: None, "existing": _existing, "unbalanced": _unbalanced}


def tree_sha256(base: Path) -> dict[str, str]:
    return {k: hashlib.sha256(v).hexdigest() for k, v in tree(base).items()}


@pytest.mark.parametrize("case", PARITY, ids=[c["id"] for c in PARITY])
def test_install_snapshot(tmp_path, case):
    proj = tmp_path / "proj"
    proj.mkdir()
    setup = SETUPS[case["setup"]]
    if setup:
        setup(proj)
    proc = run_cli(case["argv"], cwd=tmp_path)
    assert proc.returncode == case["returncode"]
    assert proc.stdout.replace(f"carcara {__version__}:", "carcara VERSION:") == case["stdout"]
    assert proc.stderr == case["stderr"]
    assert tree_sha256(proj) == case["tree_sha256"]


# --- behaviours from the retired tests/run.sh, via the CLI subprocess -------

AGENTS = ["explorer", "architect", "implementer", "test-runner", "reviewer", "doc-writer"]
COMMANDS = ["sdlc", "sdlc-plan", "sdlc-build", "sdlc-test", "sdlc-review"]


def run_cli(argv, cwd, check=False):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run(
        [sys.executable, "-m", "carcara", *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def model_of(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("model: "):
            return line[len("model: ") :]
    return ""


def test_fresh_install_contents(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    run_cli(["d"], cwd=tmp_path, check=True)
    for a in AGENTS:
        assert (d / ".claude" / "agents" / f"{a}.md").is_file()
    for c in COMMANDS:
        assert (d / ".claude" / "commands" / f"{c}.md").is_file()
    json.loads((d / ".claude" / "settings.json").read_text())
    assert (d / "CLAUDE.md").is_file()
    assert all(b"{{" not in v for v in tree(d).values())
    for a in AGENTS:
        lines = (d / ".claude" / "agents" / f"{a}.md").read_text().splitlines()
        assert lines[0] == "---"
        assert f"name: {a}" in lines
        assert any(line.startswith("description: ") and len(line) > 13 for line in lines)
        assert any(line.startswith("tools: ") and len(line) > 7 for line in lines)
        assert model_of(d / ".claude" / "agents" / f"{a}.md")


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            [],
            {
                "architect": "opus",
                "implementer": "sonnet",
                "explorer": "haiku",
                "test-runner": "haiku",
            },
        ),
        (["--profile=quality"], {"implementer": "opus", "test-runner": "haiku"}),
    ],
)
def test_profile_routing(tmp_path, argv, expected):
    (tmp_path / "d").mkdir()
    run_cli([*argv, "d"], cwd=tmp_path, check=True)
    agents = tmp_path / "d" / ".claude" / "agents"
    assert {a: model_of(agents / f"{a}.md") for a in expected} == expected


def test_balanced_main_model_and_profile_name(tmp_path):
    (tmp_path / "d").mkdir()
    run_cli(["d"], cwd=tmp_path, check=True)
    assert '"model": "sonnet"' in (tmp_path / "d" / ".claude" / "settings.json").read_text()
    assert "profile: balanced" in (tmp_path / "d" / "CLAUDE.md").read_text()


def test_economy_has_no_opus_installed(tmp_path):
    (tmp_path / "d").mkdir()
    run_cli(["--profile", "economy", "d"], cwd=tmp_path, check=True)
    assert all(b"opus" not in v for v in tree(tmp_path / "d" / ".claude").values())


def test_custom_profile_file(tmp_path):
    economy = (ROOT / "src" / "carcara" / "data" / "profiles" / "economy.env").read_text()
    lines = [
        "MODEL_REVIEWER=opus" if line.startswith("MODEL_REVIEWER=") else line
        for line in economy.splitlines()
    ]
    (tmp_path / "custom.env").write_text("\n".join(lines) + "\n")
    (tmp_path / "d").mkdir()
    run_cli(["-p", "custom.env", "d"], cwd=tmp_path, check=True)
    assert model_of(tmp_path / "d" / ".claude" / "agents" / "reviewer.md") == "opus"
    assert "profile: custom" in (tmp_path / "d" / "CLAUDE.md").read_text()


@pytest.mark.parametrize(
    "transform,err",
    [
        (
            lambda t: t.replace("MODEL_MAIN=sonnet", "MODEL_MAIN=so/net"),
            "invalid model for MODEL_MAIN",
        ),
        (
            lambda t: "".join(
                line for line in t.splitlines(True) if not line.startswith("MODEL_EXPLORER=")
            ),
            "MODEL_EXPLORER",
        ),
    ],
    ids=["unsafe-value", "incomplete"],
)
def test_bad_profile_file_rejected(tmp_path, transform, err):
    balanced = (ROOT / "src" / "carcara" / "data" / "profiles" / "balanced.env").read_text()
    bad = transform(balanced)
    assert bad != balanced
    (tmp_path / "bad.env").write_text(bad)
    (tmp_path / "d").mkdir()
    proc = run_cli(["-p", "bad.env", "d"], cwd=tmp_path)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.startswith("carcara: ") and err in proc.stderr
    assert list((tmp_path / "d").iterdir()) == []


def test_existing_settings_are_merged_keeping_model(tmp_path):
    d = tmp_path / "d"
    (d / ".claude").mkdir(parents=True)
    (d / ".claude" / "settings.json").write_text('{"model":"opus"}\n')
    proc = run_cli(["d"], cwd=tmp_path, check=True)
    assert "  merge      d/.claude/settings.json\n" in proc.stdout
    data = json.loads((d / ".claude" / "settings.json").read_text())
    assert data["model"] == "opus"
    assert "Bash(carcara run *)" not in data["permissions"]["allow"]
    assert "Read" in data["permissions"]["allow"]


def test_default_target_is_cwd_subprocess(tmp_path):
    run_cli([], cwd=tmp_path, check=True)
    assert (tmp_path / ".claude" / "agents" / "explorer.md").is_file()


@pytest.mark.parametrize(
    "argv,check",
    [
        (["--help"], lambda out: "usage: carcara" in out.lower()),
        (["--version"], lambda out: out == f"carcara {__version__}\n"),
        (
            ["-l"],
            lambda out: (
                [n for n in out.splitlines() if not n.startswith(" ")]
                == ["balanced", "economy", "quality"]
            ),
        ),
    ],
    ids=["help", "version", "list"],
)
def test_misc_flags_subprocess(tmp_path, argv, check):
    proc = run_cli(argv, cwd=tmp_path, check=True)
    assert check(proc.stdout)


def test_oserror_is_reported_without_traceback(tmp_path):
    import subprocess
    import sys

    target = tmp_path / "t"
    (target / ".claude").mkdir(parents=True)
    (target / ".claude" / "agents").write_text("not a dir")
    proc = subprocess.run(
        [sys.executable, "-m", "carcara", "install", str(target)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert proc.stderr.startswith("carcara: ")
    assert "Traceback" not in proc.stderr


_HOME_ERR = (
    "carcara: refusing to install into your home directory (Claude Code would load it as "
    "user-level config for every project); pass a project directory\n"
)


@pytest.mark.parametrize("sub", ["", ".claude", ".claude/agents", "cfg"])
def test_refuses_home_and_claude_config_dir(tmp_path, monkeypatch, capsys, sub):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude" / "agents").mkdir(parents=True)
    (home / "cfg").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "cfg"))
    target = home / sub if sub else home
    before = sorted(str(p) for p in home.rglob("*"))
    assert main([str(target)]) == 1
    assert capsys.readouterr().err == _HOME_ERR
    assert sorted(str(p) for p in home.rglob("*")) == before
    assert not (home / "CLAUDE.md").exists()
    # Also via cwd default and a symlink resolving to home.
    monkeypatch.chdir(target)
    assert main([]) == 1
    link = tmp_path / "link"
    link.symlink_to(target)
    assert main([str(link)]) == 1
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_project_under_home_still_installs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert main([str(proj)]) == 0
    assert (proj / "CLAUDE.md").is_file()
    assert (proj / ".claude" / "settings.json").is_file()
    assert not (home / "CLAUDE.md").exists()
