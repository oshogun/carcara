import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from carcara import hooks


def _env(**extra):
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.update(extra)
    return env


def _hook(name, data, project=None, raw=None, env=None, extra_args=()):
    argv = [sys.executable, "-m", "carcara", "hook", name, *extra_args]
    if project is not None:
        argv += ["--project", str(project)]
    proc = subprocess.run(
        argv,
        input=raw if raw is not None else json.dumps(data),
        capture_output=True,
        text=True,
        env=_env(**(env or {})),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _pre(tool, tool_input, cwd, **extra):
    data = {
        "session_id": "s",
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    data.update(extra)
    return data


def _decision(result):
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    return proj


# --- main session edits -----------------------------------------------------


def test_main_edit_inside_project_denied(project):
    out = _hook("pre-tool-use", _pre("Edit", {"file_path": "src/a.py"}, project), project)
    assert _decision(out) == "deny"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "carcara routing off" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("tool", ["Write", "MultiEdit"])
def test_main_write_tools_denied(project, tool):
    data = _pre(tool, {"file_path": str(project / "src" / "b.py")}, project)
    assert _decision(hooks.pre_tool_use(data, str(project))) == "deny"


def test_main_notebook_edit_denied(project):
    data = _pre("NotebookEdit", {"notebook_path": "nb.ipynb"}, project)
    assert _decision(hooks.pre_tool_use(data, str(project))) == "deny"


@pytest.mark.parametrize(
    "path",
    [".claude/agents/x.md", "CLAUDE.md", "claude.local.md", ".claude/skills/other/SKILL.md"],
)
def test_main_edit_exempt_paths_allowed(project, path):
    assert _hook("pre-tool-use", _pre("Write", {"file_path": path}, project), project) == {}


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/skills/carcara/SKILL.md",
        ".claude/skills/carcara/extra/notes.md",
    ],
)
def test_main_edit_routing_config_denied(project, path):
    out = _hook("pre-tool-use", _pre("Edit", {"file_path": path}, project), project)
    assert _decision(out) == "deny"


def test_case_insensitive_fs_spelling_denied(project, monkeypatch):
    """A differently-spelled path to the same project dir (a symlink alias) is
    matched via samefile, not string prefixes."""
    alias = project.parent / "proj-alias"
    alias.symlink_to(project)
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{}")
    (project / ".claude" / "agents").mkdir()
    monkeypatch.setattr(hooks.os.path, "realpath", os.path.normpath)
    for path in (f"{alias}/src/a.py", f"{alias}/.claude/settings.json"):
        data = _pre("Edit", {"file_path": path}, project)
        assert _decision(hooks.pre_tool_use(data, str(project))) == "deny", path
    data = _pre("Edit", {"file_path": f"{alias}/.claude/agents/x.md"}, project)
    assert hooks.pre_tool_use(data, str(project)) == {}


def test_routing_config_check_is_case_insensitive(project):
    for path in (".CLAUDE/Settings.JSON", ".claude/Skills/Carcara/SKILL.md"):
        data = _pre("Write", {"file_path": path}, project)
        assert _decision(hooks.pre_tool_use(data, str(project))) == "deny", path


def test_nested_claude_md_is_not_exempt(project):
    data = _pre("Write", {"file_path": "src/CLAUDE.md"}, project)
    assert _decision(hooks.pre_tool_use(data, str(project))) == "deny"


def test_main_edit_outside_project_allowed(project, tmp_path):
    data = _pre("Edit", {"file_path": str(tmp_path / "elsewhere.txt")}, project)
    assert _hook("pre-tool-use", data, project) == {}


def test_symlink_into_project_denied(project, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(project / "src")
    data = _pre("Edit", {"file_path": str(link / "a.py")}, tmp_path)
    assert _decision(hooks.pre_tool_use(data, str(project))) == "deny"


def test_main_edit_missing_path_passes(project):
    assert hooks.pre_tool_use(_pre("Edit", {}, project), str(project)) == {}


def test_other_tools_pass(project):
    assert hooks.pre_tool_use(_pre("Read", {"file_path": "src/a.py"}, project), str(project)) == {}


# --- subagents ----------------------------------------------------------------


def test_non_carcara_subagent_allowed(project):
    data = _pre(
        "Edit", {"file_path": "src/a.py"}, project, agent_id="a1", agent_type="general-purpose"
    )
    assert _hook("pre-tool-use", data, project) == {}


def test_carcara_role_without_strict_allowed(project):
    data = _pre("Edit", {"file_path": "src/a.py"}, project, agent_id="a1", agent_type="reviewer")
    assert _hook("pre-tool-use", data, project) == {}


def test_carcara_role_with_strict_policy_denied(project):
    (project / ".carcara").mkdir()
    (project / ".carcara" / "strict-policy").write_text("")
    data = _pre("Edit", {"file_path": "src/a.py"}, project, agent_id="a1", agent_type="reviewer")
    out = _hook("pre-tool-use", data, project)
    assert _decision(out) == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("carcara policy:")


@pytest.mark.parametrize("agent_type", ["general-purpose", "implementer"])
@pytest.mark.parametrize(
    "tool,tool_input",
    [
        ("Edit", {"file_path": ".claude/settings.json"}),
        ("Write", {"file_path": ".claude/settings.local.json"}),
        ("Write", {"file_path": ".claude/skills/carcara/SKILL.md"}),
        ("NotebookEdit", {"notebook_path": ".CLAUDE/Skills/Carcara/x.ipynb"}),
    ],
)
def test_subagent_cannot_write_routing_config(project, agent_type, tool, tool_input):
    data = _pre(tool, tool_input, project, agent_id="a1", agent_type=agent_type)
    out = hooks.pre_tool_use(data, str(project))
    assert _decision(out) == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == hooks.CONFIG_DENY_REASON


def test_subagent_routing_config_free_when_routing_off(project):
    (project / ".carcara").mkdir()
    (project / ".carcara" / "routing-off").write_text("")
    data = _pre(
        "Write", {"file_path": ".claude/settings.json"}, project, agent_id="a1", agent_type="x"
    )
    assert hooks.pre_tool_use(data, str(project)) == {}


def test_subagent_other_claude_files_allowed(project):
    for path in (".claude/agents/x.md", ".claude/skills/other/SKILL.md", "src/a.py"):
        data = _pre("Write", {"file_path": path}, project, agent_id="a1", agent_type="x")
        assert hooks.pre_tool_use(data, str(project)) == {}, path


def test_strict_policy_allows_implementer_edit(project):
    (project / ".carcara").mkdir()
    (project / ".carcara" / "strict-policy").write_text("")
    data = _pre("Edit", {"file_path": "src/a.py"}, project, agent_id="a1", agent_type="implementer")
    assert hooks.pre_tool_use(data, str(project)) == {}


# --- opt-outs -----------------------------------------------------------------


def test_stage_env_passes_through(project):
    data = _pre("Edit", {"file_path": "src/a.py"}, project)
    assert _hook("pre-tool-use", data, project, env={"CARCARA_STAGE": "implementer"}) == {}


def test_carcara_off_env_passes_through(project):
    data = _pre("Edit", {"file_path": "src/a.py"}, project)
    assert _hook("pre-tool-use", data, project, env={"CARCARA_OFF": "1"}) == {}


def test_carcara_off_falsy_value_keeps_routing(project, monkeypatch):
    monkeypatch.setenv("CARCARA_OFF", "0")
    assert hooks.routing_enabled(project)


def test_routing_off_file_passes_through(project):
    (project / ".carcara").mkdir()
    (project / ".carcara" / "routing-off").write_text("")
    data = _pre("Edit", {"file_path": "src/a.py"}, project)
    assert _hook("pre-tool-use", data, project) == {}


# --- Bash: carcara run approval flags -----------------------------------------


@pytest.mark.parametrize(
    "command,flag",
    [
        ("carcara run --resume x --yes", "--yes"),
        ("cd x && carcara run --use-api-key -", "--use-api-key"),
        ('bash -c "carcara run --resume x --accept-failures"', "--accept-failures"),
        ("FOO=1 python -m carcara run --resume x --ye", "--yes"),
        (".venv/bin/carcara run --allow-dirty --yes=1 -", "--yes"),
    ],
)
def test_bash_sensitive_run_flags_ask(project, command, flag):
    out = _hook("pre-tool-use", _pre("Bash", {"command": command}, project), project)
    assert _decision(out) == "ask"
    assert flag in out["hookSpecificOutput"]["permissionDecisionReason"]


SKILL_TASK = "carcara run --allow-dirty - <<'CARCARA_TASK'\nfix the --yes bug\nCARCARA_TASK"


@pytest.mark.parametrize(
    "command",
    [
        SKILL_TASK,
        SKILL_TASK + "\n",
        "carcara status --json",
        "carcara status",
        "carcara run --list",
        "carcara diff 20260102-000000-bbbb --stat",
        "carcara run --resume 20260102-000000-bbbb --reject",
        "carcara run --resume 20260102-000000-bbbb",
        'carcara run --size M --plan-only -p quality - <<"EOF"\nx\nEOF',
    ],
)
def test_bash_safe_carcara_forms_allowed(project, command):
    _write_run(project, "20260102-000000-bbbb", "awaiting_approval", "t")
    out = _hook("pre-tool-use", _pre("Bash", {"command": command}, project), project)
    assert _decision(out) == "allow"


def _skill_commands():
    """Every `carcara ...` command form the skill tells the model to run."""
    text = (
        Path(hooks.__file__).parent / "data/templates/claude/skills/carcara/SKILL.md"
    ).read_text()
    forms = set(re.findall(r"`(carcara (?:run|status|diff)[^`]*)`", text))
    forms.add(re.search(r"\n    (carcara run [^\n]*)\n", text).group(1))
    out = []
    for form in sorted(forms):
        form = form.replace("<id>", "20260102-000000-bbbb").replace(" N", " 3")
        delim = re.search(r"<<'(\w+)'", form)
        if delim:
            form += f"\nsome text\n{delim.group(1)}"
        out.append(form)
    return out


def test_skill_command_forms_classified(project):
    _write_run(project, "20260102-000000-bbbb", "needs_human", "t")
    forms = _skill_commands()
    assert len(forms) >= 9
    for form in forms:
        sensitive = ("--yes", "--accept-failures", "--max-budget-usd")
        expected = "ask" if any(f in form for f in sensitive) else "allow"
        assert hooks.classify_carcara_command(form, project)[0] == expected, form
    assert any("--feedback - <<'CARCARA_FEEDBACK'" in f for f in forms)
    assert any(f.endswith("--plan") for f in forms)


@pytest.mark.parametrize(
    "command",
    [
        "carcara run --resume 20260102-000000-bbbb --max-budget-usd 2.5",
        "carcara run --max-budget-usd=9 -",
        "carcara run --resume x --max-budget-usd 2.5 --yes",
        'carcara run --resume x --"max-budget-usd" 9',
        "carcara run --resume x --max-b\\udget-usd 9",
        "bash -c 'carcara run --resume x --max-budget 9'",
    ],
)
def test_bash_budget_flag_asks(project, command):
    out = hooks.pre_tool_use(_pre("Bash", {"command": command}, project), str(project))
    assert _decision(out) == "ask", command
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("carcara: changing the run budget")
    assert "needs your confirmation" in reason and "--max-budget-usd" in reason


@pytest.mark.parametrize("state", ["budget_exceeded", None, "{corrupt", "[]"])
def test_bash_resume_of_budget_exceeded_run_asks(project, state):
    run_id = "20260102-000000-bbbb"
    if state == "budget_exceeded":
        _write_run(project, run_id, state, "t")
    elif state is not None:
        (project / ".carcara" / "runs" / run_id).mkdir(parents=True)
        (project / ".carcara" / "runs" / run_id / "state.json").write_text(state)
    command = f"carcara run --resume {run_id}"
    out = hooks.pre_tool_use(_pre("Bash", {"command": command}, project), str(project))
    assert _decision(out) == "ask"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == hooks.BUDGET_RESUME_REASON
    assert hooks.classify_carcara_command(command)[0] == "ask"  # no project known


def test_bash_resume_dot_run_id_asks(project):
    (project / ".carcara" / "runs").mkdir(parents=True)
    (project / ".carcara" / "state.json").write_text('{"status": "done"}')
    assert hooks.classify_carcara_command("carcara run --resume ..", project)[0] == "ask"


@pytest.mark.parametrize(
    "command",
    [
        'carcara run --resume x --"yes"',
        'carcara run --resume x -""-yes',
        "F=--yes; carcara run $F",
        '"carcara" run --resume x --yes',
        "python -m carcara run --resume x --yes",
        "python3 -m carcara run --resume x --yes",
        "bash -c 'carcara run --resume x --yes'",
        "carcara run --resume x --y\\es",
    ],
)
def test_bash_obfuscated_sensitive_flags_ask(project, command):
    out = hooks.pre_tool_use(_pre("Bash", {"command": command}, project), str(project))
    assert _decision(out) == "ask"
    assert "--yes" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "carcara run -",
        "carcara run - <<CARCARA_TASK\nx\nCARCARA_TASK",
        "carcara run - <<'CARCARA_TASK'\nx\nCARCARA_TASK\nrm -rf /\nCARCARA_TASK",
        "carcara run - <<'T'\nx\nT\necho hi",
        "carcara run --allow-dirty fix-it",
        "carcara run --cwd /tmp -",
        "carcara run --project-settings - <<'T'\nx\nT",
        "carcara status --json; rm -rf /",
        "carcara status --json && echo ok",
        "carcara status $(id)",
        "carcara status --json > out.txt",
        "carcara diff a b",
        "carcara status ../x",
        "carcara install --force",
        "FOO=1 carcara status",
        "carcara status 'unterminated",
        "c\\arcara status",
        'ca"rc"ara status;id',
        "carcara run --feedback x --resume y",
    ],
)
def test_bash_unrecognised_carcara_forms_ask(project, command):
    out = hooks.pre_tool_use(_pre("Bash", {"command": command}, project), str(project))
    assert _decision(out) == "ask", command


@pytest.mark.parametrize(
    "command",
    [
        "~/.local/bin/carcara run --resume x --yes",
        ".venv/bin/carcara status --json",
        "env FOO=1 carcara run --resume x --yes",
        'eval "carcara run --resume x --yes"',
        'eval "carcara status"',
        "echo carcara | xargs -I{} {} run --resume x --yes",
        "time carcara status",
        "sudo -E carcara status",
        "bash -c 'carcara status'",
        "bash -lc 'carcara status'",
        "/bin/sh -c 'carcara status'",
        "sudo bash -c 'carcara status'",
        "bash -o pipefail -c 'carcara status'",
        'echo "$(carcara status)"',
        "echo `carcara status`",
        "ls; FOO=1 carcara status",
        "python3.12 -m carcara status",
        "Carcara status",
    ],
)
def test_bash_carcara_invocations_ask(project, command):
    assert hooks.classify_carcara_command(command)[0] == "ask"
    out = hooks.pre_tool_use(_pre("Bash", {"command": command}, project), str(project))
    assert _decision(out) == "ask", command


@pytest.mark.parametrize(
    "command",
    [
        "git commit --yes",
        "ls --",
        "echo hi",
        "cd /home/guilherme/carcara && .venv/bin/pytest -q",
        "cd /home/guilherme/carcara && git status",
        "ls src/carcara",
        'git commit -m "fix carcara hooks"',
        'git commit -m "carcara hooks"',
        "grep -r carcara .",
        "grep -c carcara README.md",
        "git log -c carcara",
    ],
)
def test_bash_commands_without_carcara_pass(project, command):
    assert hooks.classify_carcara_command(command) is None
    assert hooks.pre_tool_use(_pre("Bash", {"command": command}, project), str(project)) == {}


def test_bash_sensitive_flags_ask_from_subagent(project):
    data = _pre(
        "Bash",
        {"command": "carcara run --resume x --yes"},
        project,
        agent_id="a1",
        agent_type="general-purpose",
    )
    assert _decision(_hook("pre-tool-use", data, project)) == "ask"


def test_bash_safe_form_from_subagent_not_auto_allowed(project):
    data = _pre(
        "Bash", {"command": SKILL_TASK}, project, agent_id="a1", agent_type="general-purpose"
    )
    assert hooks.pre_tool_use(data, str(project)) == {}


def test_bash_safe_form_from_strict_readonly_role_still_denied(project):
    (project / ".carcara").mkdir()
    (project / ".carcara" / "strict-policy").write_text("")
    data = _pre("Bash", {"command": SKILL_TASK}, project, agent_id="a1", agent_type="reviewer")
    assert _decision(hooks.pre_tool_use(data, str(project))) == "deny"


def test_bash_ask_kept_when_routing_off(project):
    data = _pre("Bash", {"command": "carcara run --resume x --yes"}, project)
    out = _hook("pre-tool-use", data, project, env={"CARCARA_OFF": "1"})
    assert _decision(out) == "ask"
    (project / ".carcara").mkdir()
    (project / ".carcara" / "routing-off").write_text("")
    assert _decision(hooks.pre_tool_use(data, str(project))) == "ask"


def test_bash_safe_form_routing_off_takes_normal_path(project, monkeypatch):
    monkeypatch.setenv("CARCARA_OFF", "1")
    data = _pre("Bash", {"command": "carcara status --json"}, project)
    assert hooks.pre_tool_use(data, str(project)) == {}


def test_bash_carcara_stage_passes_through(project, monkeypatch):
    monkeypatch.setenv("CARCARA_STAGE", "implementer")
    data = _pre("Bash", {"command": "carcara run --resume x --yes"}, project)
    assert hooks.pre_tool_use(data, str(project)) == {}


# --- Skill tool -------------------------------------------------------------------


def test_skill_carcara_allowed(project):
    out = _hook("pre-tool-use", _pre("Skill", {"skill": "carcara"}, project), project)
    assert _decision(out) == "allow"


@pytest.mark.parametrize("skill", ["other", "carcara-x", None])
def test_skill_other_passes(project, skill):
    assert hooks.pre_tool_use(_pre("Skill", {"skill": skill}, project), str(project)) == {}


def test_skill_carcara_not_allowed_when_routing_off_or_subagent(project, monkeypatch):
    data = _pre("Skill", {"skill": "carcara"}, project, agent_id="a1", agent_type="x")
    assert hooks.pre_tool_use(data, str(project)) == {}
    monkeypatch.setenv("CARCARA_OFF", "1")
    assert hooks.pre_tool_use(_pre("Skill", {"skill": "carcara"}, project), str(project)) == {}


# --- prompt context -----------------------------------------------------------


def _write_run(project, run_id, status, task):
    run_dir = project / ".carcara" / "runs" / run_id
    run_dir.mkdir(parents=True)
    state = {"run_id": run_id, "status": status, "task": task}
    (run_dir / "state.json").write_text(json.dumps(state))


def _context(out):
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return out["hookSpecificOutput"]["additionalContext"]


def test_prompt_context_without_runs(project):
    out = _hook("prompt-context", {"prompt": "hi", "cwd": str(project)}, project)
    assert _context(out) == hooks.ROUTING_LINE
    assert not (project / ".carcara").exists()


def test_prompt_context_lists_paused_and_active_runs(project):
    _write_run(project, "20260101-000000-aaaa", "done", "old done")
    _write_run(project, "20260102-000000-bbbb", "awaiting_approval", "add\nfeature " + "x" * 200)
    _write_run(project, "20260103-000000-cccc", "needs_human", "fix bug")
    _write_run(project, "20260104-000000-dddd", "running", "live")
    bad = project / ".carcara" / "runs" / "20260105-000000-eeee"
    bad.mkdir()
    (bad / "state.json").write_text("{not json")
    (project / ".carcara" / "active.json").write_text(
        json.dumps({"pid": os.getpid(), "run_id": "20260104-000000-dddd", "started": "now"})
    )
    text = _context(hooks.prompt_context({}, str(project)))
    lines = text.splitlines()
    assert lines[0] == hooks.ROUTING_LINE
    assert "active run: 20260104-000000-dddd" in lines
    assert "20260103-000000-cccc needs_human: fix bug" in lines
    paused = next(line for line in lines if line.startswith("20260102-000000-bbbb"))
    assert paused == "20260102-000000-bbbb awaiting_approval: add feature " + "x" * 68
    assert lines.index("20260103-000000-cccc needs_human: fix bug") < lines.index(paused)
    assert "old done" not in text
    assert "`carcara status <id>`" in text
    assert len(text) < 1000


def test_prompt_context_caps_runs_and_length(project):
    for i in range(15):
        _write_run(project, f"202601{i + 10:02d}-000000-aaaa", "budget_exceeded", "t" * 300)
    text = _context(hooks.prompt_context({}, str(project)))
    assert 1 <= text.count("budget_exceeded") <= 10
    assert "20260124-000000-aaaa" in text and "20260110-000000-aaaa" not in text
    assert text.endswith("`carcara status <id>`")


def test_prompt_context_caps_at_ten_runs(project):
    for i in range(15):
        _write_run(project, f"202601{i + 10:02d}-000000-aaaa", "needs_human", "t")
    text = _context(hooks.prompt_context({}, str(project)))
    assert text.count("needs_human") == 10
    assert "20260124-000000-aaaa" in text and "20260114-000000-aaaa" not in text
    assert len(text) < 1000


def test_prompt_context_routing_off_or_stage(project):
    assert _hook("prompt-context", {}, project, env={"CARCARA_OFF": "yes"}) == {}
    assert _hook("prompt-context", {}, project, env={"CARCARA_STAGE": "main"}) == {}


# --- CLI robustness -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", '"str"'])
def test_malformed_stdin_exit0_silent(project, raw):
    assert _hook("pre-tool-use", None, project, raw=raw) == {}


def test_bad_hook_name_exit0_silent(project):
    assert _hook("bogus", {}, project) == {}
    assert _hook("pre-tool-use", {}, project, extra_args=("--nope",)) == {}


def test_odd_input_types_pass_through(project):
    data = _pre("Edit", {"file_path": 123}, project)
    assert _hook("pre-tool-use", data, project) == {}
    assert _hook("pre-tool-use", {"tool_name": "Edit", "tool_input": "x"}, project) == {}


def test_handler_exception_is_swallowed(monkeypatch, capsys):
    def boom(data, project):
        raise RuntimeError("boom")

    monkeypatch.setitem(hooks.HANDLERS, "pre-tool-use", boom)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("{}"))
    assert hooks.hook_main(["pre-tool-use", "--project", "."]) == 0
    assert capsys.readouterr().out == ""


def test_project_defaults_to_claude_project_dir(project, tmp_path):
    data = _pre("Edit", {"file_path": str(project / "src" / "a.py")}, tmp_path)
    out = _hook("pre-tool-use", data, env={"CLAUDE_PROJECT_DIR": str(project)})
    assert _decision(out) == "deny"


def test_project_defaults_to_data_cwd(project):
    out = _hook("pre-tool-use", _pre("Edit", {"file_path": "src/a.py"}, project))
    assert _decision(out) == "deny"


def test_hook_subprocess_is_fast(project):
    data = _pre("Edit", {"file_path": "src/a.py"}, project)
    _hook("pre-tool-use", data, project)  # warm caches
    start = time.monotonic()
    _hook("pre-tool-use", data, project)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"hook took {elapsed:.2f}s (target < 1s)"


# --- carcara routing on|off|status --------------------------------------------


def _routing(*args):
    return subprocess.run(
        [sys.executable, "-m", "carcara", "routing", *args],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=30,
    )


def test_routing_on_off_status(project):
    out = _routing("status", "--project", str(project))
    assert out.returncode == 0 and out.stdout.strip() == "routing: on"
    out = _routing("off", "--project", str(project))
    assert out.returncode == 0 and "routing: off" in out.stdout
    assert ".carcara/routing-off" in out.stdout
    assert (project / ".carcara" / "routing-off").exists()
    assert (project / ".carcara" / ".gitignore").read_text() == "*\n"
    assert not hooks.routing_enabled(project)
    out = _routing("on", "--project", str(project))
    assert out.returncode == 0 and out.stdout.strip() == "routing: on"
    assert not (project / ".carcara" / "routing-off").exists()


def test_routing_status_reports_env(project, monkeypatch):
    monkeypatch.setenv("CARCARA_OFF", "1")
    out = _routing("status", "--project", str(project))
    assert out.stdout.strip() == "routing: off (CARCARA_OFF is set)"


def test_routing_bad_action_errors(project):
    assert _routing("maybe", "--project", str(project)).returncode == 2
