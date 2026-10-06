"""Routing install: carcara skill, settings.json merge, --no-routing, --strict-policy."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from carcara.cli import main
from carcara.installer import (
    LEGACY_ALLOW,
    ROUTING_OFF_TEXT,
    ROUTING_ON_TEXT,
    STRICT_MATCHERS,
    hook_command,
    hook_group,
    is_carcara_group,
)
from carcara.resources import templates_root

SKILL = templates_root().joinpath("claude", "skills", "carcara", "SKILL.md")
SETTINGS = templates_root().joinpath("claude", "settings.json")
DEFAULT_PRE = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Skill"]


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    return d


def settings_of(d: Path) -> dict:
    return json.loads((d / ".claude" / "settings.json").read_text())


def pre_matchers(data: dict, carcara: bool = True) -> list:
    groups = data.get("hooks", {}).get("PreToolUse", [])
    return [g.get("matcher") for g in groups if is_carcara_group(g) == carcara]


def carcara_groups(data: dict) -> list:
    return [g for gs in data.get("hooks", {}).values() for g in gs if is_carcara_group(g)]


# --- skill template -----------------------------------------------------------


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    assert text.startswith("---\n")
    head, body = text[4:].split("\n---\n", 1)
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        assert sep and key and value, line
        fields[key] = value
    return fields, body


def test_skill_frontmatter():
    fields, body = frontmatter(SKILL.read_text())
    assert fields["name"] == "carcara"
    assert len(fields["description"]) + len(fields["when_to_use"]) < 1536
    # No allowed-tools: it would pre-approve e.g. `carcara run --yes` for the turn,
    # bypassing the hook that asks the human for approval/billing flags.
    assert "allowed-tools" not in fields
    for value in fields.values():
        # Plain YAML scalars: one line, no mapping/comment indicators, safe first char.
        assert ": " not in value and " #" not in value and not value.endswith(":")
        assert value[0] not in "-?:,[]{}#&*!|>'\"%@`"
    assert body.startswith("<!-- carcara:skill")


def test_skill_protocol():
    _, body = frontmatter(SKILL.read_text())
    for code in ("**0 done", "**3 awaiting_approval", "**4 needs_human", "**5 budget", "**6 busy"):
        assert code in body
    assert "carcara run --allow-dirty - <<'CARCARA_TASK'" in body
    assert "\n    CARCARA_TASK\n" in body
    assert "run_in_background: true" in body
    assert "carcara status --json" in body
    assert "<<'CARCARA_FEEDBACK'" in body
    budget = body[body.index("**5 budget") : body.index("**6 busy")]
    assert "--max-budget-usd" in budget and "prompts the user for confirmation" in budget
    assert "AskUserQuestion" in body
    for flag in ("--yes", "--accept-failures", "--use-api-key"):
        assert flag in body


# --- settings template and hook command -----------------------------------------


def test_settings_template_hooks_match_installer():
    data = json.loads(SETTINGS.read_text())
    assert data["hooks"] == {
        "UserPromptSubmit": [hook_group("prompt-context")],
        "PreToolUse": [hook_group("pre-tool-use", m) for m in DEFAULT_PRE],
    }
    # The hook is the sole approver of carcara commands: no allow rules for them.
    assert not [a for a in data["permissions"]["allow"] if "carcara" in a.lower()]
    assert not [a for a in data["permissions"]["allow"] if a.startswith("Skill")]
    for group in carcara_groups(data):
        assert "args" not in group["hooks"][0]  # no args: runs through a shell


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_hook_command_shell(tmp_path, shell):
    sh = shutil.which(shell)
    if not sh:
        pytest.skip(f"needs {shell}")
    cmd = hook_command("pre-tool-use")
    assert subprocess.run([sh, "-n", "-c", cmd]).returncode == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {"PATH": str(empty), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    proc = subprocess.run([sh, "-c", cmd], env=env, capture_output=True, text=True)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "carcara"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake.chmod(0o755)
    env = {"PATH": str(bindir), "CLAUDE_PROJECT_DIR": str(tmp_path / "my proj")}
    proc = subprocess.run([sh, "-c", cmd], env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == [
        "hook",
        "pre-tool-use",
        "--project",
        str(tmp_path / "my proj"),
    ]


# --- settings merge -------------------------------------------------------------

USER_SETTINGS = {
    "model": "haiku",
    "permissions": {
        "allow": ["Bash(make:*)", "Read"],
        "deny": ["Read(./private/**)"],
    },
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard"}]},
            # Stale carcara group (older command form): replaced, not duplicated.
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "carcara hook old"}]},
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-me"}]}],
    },
    "env": {"FOO": "1"},
}


def write_settings(d: Path, data) -> bytes:
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    (d / ".claude" / "settings.json").write_bytes(raw)
    return raw


def test_merge_preserves_user_settings(proj, capsys):
    write_settings(proj, USER_SETTINGS)
    assert main(["d"]) == 0
    assert "  merge      d/.claude/settings.json\n" in capsys.readouterr().out
    data = settings_of(proj)
    assert list(data) == ["model", "permissions", "hooks", "env"]
    assert data["model"] == "haiku"
    assert data["env"] == {"FOO": "1"}
    allow = data["permissions"]["allow"]
    assert allow[:2] == ["Bash(make:*)", "Read"]
    assert allow.count("Read") == 1 and not set(LEGACY_ALLOW) & set(allow)
    assert data["permissions"]["deny"][0] == "Read(./private/**)"
    assert "Read(./.env)" in data["permissions"]["deny"]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == USER_SETTINGS["hooks"]["PreToolUse"][0]
    assert pre[1:] == [hook_group("pre-tool-use", m) for m in DEFAULT_PRE]
    assert data["hooks"]["Stop"] == USER_SETTINGS["hooks"]["Stop"]
    assert data["hooks"]["UserPromptSubmit"] == [hook_group("prompt-context")]

    first = (proj / ".claude" / "settings.json").read_bytes()
    assert first.endswith(b"}\n") and b'\n  "model"' in first  # indent 2
    assert main(["d"]) == 0
    out = capsys.readouterr().out
    assert "  up-to-date d/.claude/settings.json\n" in out
    assert (proj / ".claude" / "settings.json").read_bytes() == first


def test_fresh_install_is_up_to_date_on_reinstall(proj, capsys):
    assert main(["d"]) == 0
    first = (proj / ".claude" / "settings.json").read_bytes()
    assert main(["d"]) == 0
    assert "  up-to-date d/.claude/settings.json\n" in capsys.readouterr().out
    assert (proj / ".claude" / "settings.json").read_bytes() == first


def test_force_sets_model(proj):
    write_settings(proj, USER_SETTINGS)
    assert main(["--force", "-p", "quality", "d"]) == 0
    data = settings_of(proj)
    assert data["model"] == "opus"
    assert data["env"] == {"FOO": "1"}


def test_dry_run_merge_writes_nothing(proj, capsys):
    raw = write_settings(proj, USER_SETTINGS)
    assert main(["-n", "d"]) == 0
    assert "  merge      d/.claude/settings.json\n" in capsys.readouterr().out
    assert (proj / ".claude" / "settings.json").read_bytes() == raw
    assert os.listdir(proj / ".claude") == ["settings.json"]


@pytest.mark.parametrize(
    "content,err",
    [
        (b"{not json", "is not valid JSON"),
        (b"[]", "expected a JSON object"),
        (b'{"permissions": {"allow": "Read"}}', '"permissions.allow" must be a list'),
        (b'{"hooks": {"PreToolUse": {}}}', '"hooks.PreToolUse" must be a list of objects'),
    ],
)
def test_bad_settings_fail_before_any_write(proj, capsys, content, err):
    write_settings(proj, content)
    assert main(["d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("carcara: d/.claude/settings.json") and err in captured.err
    assert sorted(os.listdir(proj)) == [".claude"]
    assert os.listdir(proj / ".claude") == ["settings.json"]
    assert (proj / ".claude" / "settings.json").read_bytes() == content


# --- CLAUDE.md routing text ---------------------------------------------------------


def test_claude_md_routing_text(proj, capsys):
    assert main(["d"]) == 0
    text = (proj / "CLAUDE.md").read_text()
    assert ROUTING_ON_TEXT + "\n- Delegate searching" in text
    assert "Triage first" not in text and "{{" not in text
    assert "`carcara run" in text
    assert main(["--no-routing", "d"]) == 0
    text = (proj / "CLAUDE.md").read_text()
    assert (
        "### Token discipline\n"
        "- Triage first: do S-sized changes directly; only escalate to subagents when\n"
        "  the task warrants it. Use the architect only for L-sized work.\n"
        "- Delegate searching"
    ) in text
    assert ROUTING_OFF_TEXT in text and ROUTING_ON_TEXT not in text


# --- --no-routing / --strict-policy ---------------------------------------------------


def test_no_routing_fresh(proj, capsys):
    assert main(["--no-routing", "d"]) == 0
    out = capsys.readouterr().out
    assert "SKILL.md" not in out
    assert out.endswith("Next: start Claude Code in d and run: /sdlc <task>\n")
    assert "done: 12 file(s) written, 0 skipped.\n" in out
    assert not (proj / ".claude" / "skills").exists()
    data = settings_of(proj)
    assert "hooks" not in data
    assert not set(LEGACY_ALLOW) & set(data["permissions"]["allow"])
    # Byte-identical to the 0.2.0 (pre-routing) install.
    assert {
        name: hashlib.sha256((proj / name).read_bytes()).hexdigest()
        for name in (".claude/settings.json", "CLAUDE.md")
    } == {
        ".claude/settings.json": "96562bd79c5a57c4e96f921ae11cec07850cdf0f16360cb912a14d9251d14e4a",
        "CLAUDE.md": "e69543840f43969639110ea57429d5389be840667fbaff14d2357a282bed5e99",
    }


def test_no_routing_strips_carcara_parts(proj, capsys):
    write_settings(proj, USER_SETTINGS)
    assert main(["d"]) == 0
    assert (proj / ".claude" / "skills" / "carcara" / "SKILL.md").is_file()
    capsys.readouterr()
    assert main(["--no-routing", "d"]) == 0
    out = capsys.readouterr().out
    assert "  remove     d/.claude/skills/carcara/SKILL.md\n" in out
    assert not (proj / ".claude" / "skills").exists()
    data = settings_of(proj)
    assert carcara_groups(data) == []
    assert data["hooks"] == {
        "PreToolUse": USER_SETTINGS["hooks"]["PreToolUse"][:1],
        "Stop": USER_SETTINGS["hooks"]["Stop"],
    }
    assert data["permissions"]["allow"][:2] == ["Bash(make:*)", "Read"]
    assert not set(LEGACY_ALLOW) & set(data["permissions"]["allow"])
    # Routing back on restores the same file as the first install.
    assert main(["d"]) == 0
    data = settings_of(proj)
    assert pre_matchers(data) == DEFAULT_PRE
    assert pre_matchers(data, carcara=False) == ["Bash"]
    assert data["hooks"]["UserPromptSubmit"] == [hook_group("prompt-context")]


def test_no_routing_keeps_user_skill(proj):
    skill = proj / ".claude" / "skills" / "carcara" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: carcara\ndescription: mine\n---\nmy own skill\n")
    assert main(["--no-routing", "d"]) == 0
    assert skill.read_text().endswith("my own skill\n")


def test_no_routing_hooks_emptied_by_carcara_are_dropped(proj):
    assert main(["d"]) == 0
    assert main(["--no-routing", "d"]) == 0
    data = settings_of(proj)
    assert "hooks" not in data
    raw = (proj / ".claude" / "settings.json").read_bytes()
    assert main(["--no-routing", "d"]) == 0
    assert (proj / ".claude" / "settings.json").read_bytes() == raw


def test_strict_policy_adds_and_removes(proj, capsys):
    assert main(["d"]) == 0
    default = (proj / ".claude" / "settings.json").read_bytes()
    capsys.readouterr()
    assert main(["--strict-policy", "d"]) == 0
    out = capsys.readouterr().out
    assert "  create     d/.carcara/strict-policy\n" in out
    assert pre_matchers(settings_of(proj)) == DEFAULT_PRE + list(STRICT_MATCHERS)
    assert (proj / ".carcara" / "strict-policy").is_file()
    assert (proj / ".carcara" / ".gitignore").read_text() == "*\n"
    assert main(["--strict-policy", "d"]) == 0
    assert "up-to-date d/.claude/settings.json" in capsys.readouterr().out
    assert main(["d"]) == 0
    assert "  remove     d/.carcara/strict-policy\n" in capsys.readouterr().out
    assert not (proj / ".carcara" / "strict-policy").exists()
    assert (proj / ".carcara" / ".gitignore").exists()
    assert (proj / ".claude" / "settings.json").read_bytes() == default


def test_strict_policy_requires_routing(proj, capsys):
    assert main(["--strict-policy", "--no-routing", "d"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot be combined" in captured.err
    assert os.listdir(proj) == []


# --- legacy allow rules / mixed hook groups / atomic write ----------------------


@pytest.mark.parametrize("routing", [True, False])
def test_legacy_carcara_allow_rules_removed(proj, routing):
    user = {"permissions": {"allow": ["Bash(make:*)", *LEGACY_ALLOW, "Bash(carcara run:*)"]}}
    write_settings(proj, user)
    assert main(["d"] if routing else ["--no-routing", "d"]) == 0
    allow = settings_of(proj)["permissions"]["allow"]
    assert allow[:2] == ["Bash(make:*)", "Bash(carcara run:*)"]  # user's own rule kept
    assert not set(LEGACY_ALLOW) & set(allow)


def test_mixed_group_keeps_user_hooks(proj, capsys):
    mixed = {
        "matcher": "Bash",
        "hooks": [
            {"type": "command", "command": "my-guard"},
            {"type": "command", "command": "carcara hook pre-tool-use --old"},
        ],
    }
    write_settings(proj, {"hooks": {"PreToolUse": [mixed]}})
    assert main(["d"]) == 0
    pre = settings_of(proj)["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard"}]}
    assert pre[1:] == [hook_group("pre-tool-use", m) for m in DEFAULT_PRE]
    capsys.readouterr()
    assert main(["d"]) == 0
    assert "  up-to-date d/.claude/settings.json\n" in capsys.readouterr().out
    assert main(["--no-routing", "d"]) == 0
    assert settings_of(proj)["hooks"] == {"PreToolUse": [pre[0]]}


def test_settings_written_atomically_keeping_mode(proj, monkeypatch):
    raw = write_settings(proj, USER_SETTINGS)
    path = proj / ".claude" / "settings.json"
    path.chmod(0o640)
    assert main(["d"]) == 0
    assert path.stat().st_mode & 0o777 == 0o640
    assert sorted(os.listdir(proj / ".claude")) == ["agents", "commands", "settings.json", "skills"]

    path.write_bytes(raw)
    import carcara.installer as inst

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(inst.os, "replace", boom)
    with pytest.raises(OSError):
        inst._write_atomic(str(path), b"{}\n")
    assert path.read_bytes() == raw  # original untouched, temp file cleaned up
    assert [n for n in os.listdir(proj / ".claude") if n.endswith(".tmp")] == []


def test_symlinked_settings_stay_a_symlink(proj, tmp_path):
    real = tmp_path / "shared-settings.json"
    real.write_text(json.dumps(USER_SETTINGS))
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").symlink_to(real)
    assert main(["d"]) == 0
    assert (proj / ".claude" / "settings.json").is_symlink()
    assert pre_matchers(json.loads(real.read_text())) == DEFAULT_PRE
