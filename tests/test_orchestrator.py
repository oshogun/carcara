import asyncio
import json
import os
import subprocess

import pytest

from carcara.backend import BackendError, FakeBackend, StageResult
from carcara.orchestrator import (
    AutoGate,
    Orchestrator,
    OrchestratorError,
    RunOptions,
)
from carcara.policy import WRITE_TOOLS
from carcara.profiles import load_profile
from carcara.roles import model_for
from carcara.runstore import RunStore

PROFILE = load_profile("balanced")

TRIAGE_S = {"size": "S", "rationale": "small"}
EXPLORE = {"summary": "found", "findings": [{"path": "a.py", "line": 1, "fact": "x"}]}
PLAN = {
    "goal": "g",
    "steps": [
        {"id": "step-1", "files": ["a.py"], "change": "one"},
        {"id": "step-2", "files": ["b.py"], "change": "two"},
    ],
    "tests": ["t"],
    "acceptance": ["a"],
    "risks": [],
}


def impl(path="a.py", blocked=False, user_facing=False):
    return {
        "changed": [{"path": path, "summary": "s"}],
        "verified": "pytest",
        "notes": "blocked on x" if blocked else "",
        "blocked": blocked,
        "user_facing_change": user_facing,
    }


TEST_OK = {"passed": True, "commands": ["pytest"], "failures": []}
TEST_FAIL = {
    "passed": False,
    "commands": ["pytest"],
    "failures": [{"name": "t1", "detail": "boom"}],
}
REVIEW_OK = {"verdict": "approve", "findings": []}
REVIEW_MAJOR = {
    "verdict": "request_changes",
    "findings": [
        {"severity": "major", "path": "a.py", "issue": "bug", "fix": "fix it"},
        {"severity": "nit", "path": "a.py", "issue": "style", "fix": "meh"},
    ],
}


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


def make(repo, script, gate=None, costs=None, **opts):
    backend = FakeBackend(script, costs)
    gate = gate or AutoGate()
    orch = Orchestrator(backend, PROFILE, str(repo), RunStore(repo), gate, RunOptions(**opts))
    return orch, backend, gate


def go(orch, task="do it"):
    return asyncio.run(orch.run(task))


def seq(backend):
    return [(r.stage, r.role) for r in backend.requests]


def keys(orch):
    return [s["key"] for s in orch.run_state.state["stages"]]


def check_requests(backend, **expect):
    for req in backend.requests:
        assert req.model == model_for(req.role or "main", PROFILE)
        if req.role not in ("implementer", "doc-writer"):
            assert not WRITE_TOOLS & set(req.allowed_tools), req
        assert req.setting_sources == expect.get("setting_sources", [])
        assert req.cwd == expect["cwd"]


def test_small_sequence(repo):
    orch, backend, _ = make(repo, {"triage": [TRIAGE_S], "implement": [impl()], "test": [TEST_OK]})
    out = go(orch)
    assert (out.status, out.exit_code) == ("done", 0)
    assert seq(backend) == [
        ("triage", None),
        ("implement", "implementer"),
        ("test", "test-runner"),
    ]
    assert backend.requests[0].allowed_tools == ["StructuredOutput"]
    assert backend.requests[1].permission_mode == "acceptEdits"
    assert backend.requests[2].permission_mode == "dontAsk"
    check_requests(backend, cwd=str(repo))
    assert all(r.max_budget_usd is None for r in backend.requests)
    assert backend.requests[0].max_turns == 3
    report = (orch.run_state.dir / "report.md").read_text()
    assert report == out.report_text
    assert len(report.splitlines()) <= 10
    assert "a.py" in report and "passed" in report
    assert (repo / ".carcara" / ".gitignore").read_text() == "*\n"
    events = (orch.run_state.dir / "events.jsonl").read_text().splitlines()
    assert json.loads(events[0])["event"] == "run_started"


def test_small_with_review_and_project_settings(repo):
    orch, backend, _ = make(
        repo,
        {"implement": [impl()], "test": [TEST_OK], "review": [REVIEW_OK]},
        size="S",
        review_small=True,
        project_settings=True,
    )
    assert go(orch).status == "done"
    assert seq(backend) == [
        ("implement", "implementer"),
        ("test", "test-runner"),
        ("review", "reviewer"),
    ]
    check_requests(backend, cwd=str(repo), setting_sources=["project"])


def test_medium_sequence(repo):
    orch, backend, gate = make(
        repo,
        {
            "explore": [EXPLORE],
            "plan": [PLAN],
            "implement": [impl()],
            "test": [TEST_OK],
            "review": [REVIEW_OK],
        },
        size="M",
    )
    assert go(orch).status == "done"
    assert seq(backend) == [
        ("explore", "explorer"),
        ("plan", None),
        ("implement", "implementer"),
        ("test", "test-runner"),
        ("review", "reviewer"),
    ]
    assert gate.plans == []  # no gate for M without approve_plan
    assert '"goal":"g"' in backend.requests[2].prompt
    assert '"fact":"x"' in backend.requests[1].prompt
    assert "git diff --stat" in backend.requests[4].prompt
    check_requests(backend, cwd=str(repo))


def test_large_sequence_with_docs(repo):
    orch, backend, gate = make(
        repo,
        {
            "explore": [EXPLORE],
            "plan": [PLAN],
            "implement": [impl("a.py"), impl("b.py", user_facing=True)],
            "test": [TEST_OK],
            "review": [REVIEW_OK],
            "docs": [{"changed": ["README.md"]}],
        },
        size="L",
    )
    out = go(orch)
    assert out.status == "done"
    assert seq(backend) == [
        ("explore", "explorer"),
        ("plan", "architect"),
        ("implement", "implementer"),
        ("implement", "implementer"),
        ("test", "test-runner"),
        ("review", "reviewer"),
        ("docs", "doc-writer"),
    ]
    assert keys(orch) == [
        "explore",
        "architect",
        "implement:step-1",
        "implement:step-2",
        "test",
        "review",
        "docs",
    ]
    assert gate.plans == [PLAN]
    assert "step step-2" in backend.requests[3].prompt
    assert "a.py, b.py" in out.report_text
    check_requests(backend, cwd=str(repo))


def test_large_without_user_facing_change_skips_docs(repo):
    orch, backend, _ = make(
        repo,
        {
            "triage": [{"size": "L", "rationale": "big"}],
            "explore": [EXPLORE],
            "plan": [PLAN],
            "implement": [impl(), impl()],
            "test": [TEST_OK],
            "review": [REVIEW_OK],
        },
    )
    assert go(orch).status == "done"
    assert seq(backend)[-1] == ("review", "reviewer")
    assert orch.run_state.state["size"] == "L"


def test_gate_reject(repo):
    orch, backend, _ = make(
        repo, {"explore": [EXPLORE], "plan": [PLAN]}, gate=AutoGate(approve=False), size="L"
    )
    out = go(orch)
    assert (out.status, out.exit_code) == ("failed", 1)
    assert "rejected" in out.report_text
    assert len(backend.requests) == 2


def test_medium_approve_plan_gate(repo):
    orch, _, gate = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        gate=AutoGate(decision="defer"),
        size="M",
        approve_plan=True,
    )
    assert go(orch).exit_code == 3
    assert gate.plans == [PLAN]


def test_defer_then_resume_does_not_rerun_explore_or_architect(repo):
    orch, _, _ = make(
        repo, {"explore": [EXPLORE], "plan": [PLAN]}, gate=AutoGate(decision="defer"), size="L"
    )
    out = go(orch)
    assert (out.status, out.exit_code) == ("awaiting_approval", 3)
    run_id = out.run_id
    assert f"--resume {run_id}" in out.report_text
    assert json.loads((orch.run_state.dir / "state.json").read_text())["status"] == (
        "awaiting_approval"
    )

    # Fresh backend without explore/plan entries: replaying them must not call it.
    orch2, backend2, gate2 = make(
        repo,
        {"implement": [impl(), impl()], "test": [TEST_OK], "review": [REVIEW_OK]},
    )
    out2 = asyncio.run(orch2.resume(run_id))
    assert (out2.status, out2.exit_code) == ("done", 0)
    assert [s for s, _ in seq(backend2)] == ["implement", "implement", "test", "review"]
    assert gate2.plans == [PLAN]

    # Resuming a finished run is a no-op.
    orch3, backend3, _ = make(repo, {})
    out3 = asyncio.run(orch3.resume(run_id))
    assert out3.exit_code == 0 and backend3.requests == []


def test_plan_only(repo):
    orch, backend, gate = make(
        repo, {"explore": [EXPLORE], "plan": [PLAN]}, size="L", plan_only=True
    )
    out = go(orch)
    assert (out.status, out.exit_code) == ("plan_only", 0)
    assert seq(backend) == [("explore", "explorer"), ("plan", "architect")]
    assert gate.plans == []


def test_fix_loop_success(repo):
    orch, backend, _ = make(
        repo,
        {
            "implement": [impl(), impl("b.py")],
            "test": [TEST_FAIL, TEST_OK],
            "review": [REVIEW_OK],
        },
        size="S",
        review_small=True,
    )
    assert go(orch).status == "done"
    assert keys(orch) == ["implement", "test", "fix-1:implement", "fix-1:test", "fix-1:review"]
    fix_prompt = backend.requests[2].prompt
    assert '"name":"t1"' in fix_prompt and "git diff" in fix_prompt


def test_fix_loop_only_serious_findings(repo):
    orch, backend, _ = make(
        repo,
        {
            "implement": [impl(), impl()],
            "test": [TEST_OK, TEST_OK],
            "review": [REVIEW_MAJOR, REVIEW_OK],
        },
        size="S",
        review_small=True,
    )
    assert go(orch).status == "done"
    fix_prompt = backend.requests[3].prompt
    assert '"issue":"bug"' in fix_prompt and "style" not in fix_prompt.split("git status")[0]


def test_fix_loop_cap_needs_human_then_resume_with_dirty_tree(repo):
    orch, backend, gate = make(
        repo,
        {"implement": [impl(), impl(), impl()], "test": [TEST_FAIL] * 3},
        size="S",
    )
    out = go(orch)
    assert (out.status, out.exit_code) == ("needs_human", 4)
    assert keys(orch) == [
        "implement",
        "test",
        "fix-1:implement",
        "fix-1:test",
        "fix-2:implement",
        "fix-2:test",
    ]
    assert len(gate.summaries) == 1

    (repo / "a.py").write_text("x = 2  # human fix\n")
    orch2, backend2, _ = make(repo, {"test": [TEST_OK]})
    out2 = asyncio.run(orch2.resume(out.run_id))
    assert (out2.status, out2.exit_code) == ("done", 0)
    assert seq(backend2) == [("test", "test-runner")]
    assert keys(orch2)[-1] == "retry-1:test"


def test_fix_loop_cap_continue(repo):
    orch, _, _ = make(
        repo,
        {"implement": [impl(), impl(), impl()], "test": [TEST_FAIL] * 3},
        gate=AutoGate(continue_=True),
        size="S",
    )
    out = go(orch)
    assert (out.status, out.exit_code) == ("done", 0)
    assert orch.run_state.state["accepted_failures"]


def test_blocked_implement_needs_human_and_is_rerun_on_resume(repo):
    orch, _, _ = make(repo, {"implement": [impl(blocked=True)]}, size="S")
    out = go(orch)
    assert (out.status, out.exit_code) == ("needs_human", 4)
    assert keys(orch) == []
    orch2, backend2, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]})
    assert asyncio.run(orch2.resume(out.run_id)).status == "done"
    assert len(backend2.requests) == 2


def test_budget_remaining_and_cost_totals(repo):
    orch, backend, _ = make(
        repo,
        {
            "explore": [EXPLORE],
            "plan": [PLAN],
            "implement": [impl()],
            "test": [TEST_OK],
            "review": [REVIEW_OK],
        },
        costs={"explore": 0.25, "plan": 0.5, "implement": 1.0, "test": 0.25, "review": 0.5},
        size="M",
        max_budget_usd=10.0,
    )
    out = go(orch)
    assert out.status == "done"
    assert [r.max_budget_usd for r in backend.requests] == [10.0, 9.75, 9.25, 8.25, 8.0]
    stages = orch.run_state.state["stages"]
    assert orch.run_state.state["totals"]["cost_usd"] == pytest.approx(
        sum(s["cost_usd"] for s in stages)
    )
    assert "$2.50" in out.report_text


def test_budget_exhausted_before_stage(repo):
    orch, backend, _ = make(
        repo,
        {"implement": [impl()], "test": [TEST_OK]},
        costs={"implement": 1.0},
        size="S",
        max_budget_usd=1.0,
    )
    out = go(orch)
    assert (out.status, out.exit_code) == ("budget_exceeded", 5)
    assert len(backend.requests) == 1


def test_budget_exceeded_result_subtype(repo):
    over = StageResult(subtype="error_max_budget_usd", is_error=True, cost_usd=0.7)
    orch, _, _ = make(repo, {"implement": [over]}, size="S", max_budget_usd=0.5)
    out = go(orch)
    assert (out.status, out.exit_code) == ("budget_exceeded", 5)
    assert orch.run_state.state["totals"]["cost_usd"] == pytest.approx(0.7)


def test_stage_error_fails_and_is_resumable(repo):
    err = StageResult(subtype="error_max_turns", is_error=True, errors=["too many turns"])
    orch, _, _ = make(repo, {"implement": [impl()], "test": [err]}, size="S")
    out = go(orch)
    assert (out.status, out.exit_code) == ("failed", 1)
    assert "too many turns" in out.report_text
    orch2, backend2, _ = make(repo, {"test": [TEST_OK]})
    assert asyncio.run(orch2.resume(out.run_id)).status == "done"
    assert seq(backend2) == [("test", "test-runner")]


def test_dirty_tree_refused_on_fresh_run(repo):
    (repo / "a.py").write_text("dirty\n")
    orch, backend, _ = make(repo, {})
    with pytest.raises(OrchestratorError, match="allow-dirty"):
        go(orch)
    assert backend.requests == []
    assert not (repo / ".carcara").exists()

    orch2, _, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]}, size="S", allow_dirty=True)
    assert go(orch2).status == "done"


def test_untracked_file_counts_as_dirty(repo):
    (repo / "new.py").write_text("y\n")
    orch, _, _ = make(repo, {})
    with pytest.raises(OrchestratorError):
        go(orch)


def test_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    orch, _, _ = make(tmp_path, {})
    with pytest.raises(OrchestratorError, match="not a git repository"):
        go(orch)


def test_runstore_list_and_unknown(repo):
    store = RunStore(repo)
    assert store.list_runs() == []
    orch, _, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]}, size="S")
    out = go(orch)
    assert store.list_runs() == [out.run_id]
    orch2, _, _ = make(repo, {})
    with pytest.raises(OrchestratorError, match="unknown run"):
        asyncio.run(orch2.resume("nope"))


def test_second_run_ignores_carcara_dir(repo):
    orch, _, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]}, size="S")
    assert go(orch).status == "done"
    orch2, _, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]}, size="S")
    assert go(orch2).status == "done"
    assert len(RunStore(repo).list_runs()) == 2


def test_diff_context_includes_untracked_files(repo):
    (repo / "new.py").write_text("print('hello')\n")
    (repo / "blob.bin").write_bytes(b"\x00\x01binary")
    (repo / ".env").write_text("TOKEN=secret\n")
    (repo / ".carcara").mkdir()
    (repo / ".carcara" / "x.txt").write_text("internal\n")
    orch, _, _ = make(repo, {}, allow_dirty=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    orch.run_state = orch.store.create("t", PROFILE.name, head)
    ctx = orch._diff_context()
    assert "### untracked: new.py\nprint('hello')" in ctx
    assert "### untracked: blob.bin\n(skipped: binary)" in ctx
    assert "TOKEN=secret" not in ctx
    assert ".carcara" not in ctx.split("### git diff", 1)[1]


def test_untracked_context_respects_cap(repo, monkeypatch):
    import carcara.orchestrator as mod

    monkeypatch.setattr(mod, "DIFF_CAP", 200)
    (repo / "big.txt").write_text("y" * 1000)
    (repo / "small.txt").write_text("ok\n")
    text = mod._untracked_context(str(repo), 200)
    assert "### untracked: big.txt\n(skipped: too large)" in text
    assert "### untracked: small.txt\nok" in text


def test_backend_error_mid_stage_fails_without_cost(repo):
    orch, _, _ = make(repo, {"implement": [impl()], "test": []}, size="S", costs={"implement": 0.2})
    out = go(orch)
    assert (out.status, out.exit_code) == ("failed", 1)
    assert "script exhausted" in out.report_text
    assert orch.run_state.state["totals"]["cost_usd"] == pytest.approx(0.2)


@pytest.mark.parametrize(
    "result,expected",
    [
        (StageResult(subtype="success", is_error=True, text="API overloaded"), "API overloaded"),
        (StageResult(subtype="success", is_error=True), "error result (subtype success)"),
    ],
)
def test_stage_error_detail_fallbacks(repo, result, expected):
    orch, _, _ = make(repo, {"implement": [result]}, size="S")
    out = go(orch)
    assert out.status == "failed"
    assert f"stage implement failed: {expected}" in out.report_text
    assert "failed: success" not in out.report_text


class _FailingBackend(FakeBackend):
    """Raises a BackendError carrying a costed StageResult for the test stage."""

    async def run_stage(self, request):
        if request.stage == "test":
            self.requests.append(request)
            spent = StageResult(cost_usd=0.008, usage={"input_tokens": 7}, num_turns=4)
            raise BackendError("stage test: no structured output", spent)
        return await super().run_stage(request)


def test_failed_stage_cost_counted_in_totals(repo):
    backend = _FailingBackend({"implement": [impl()]}, {"implement": 0.01})
    orch = Orchestrator(
        backend, PROFILE, str(repo), RunStore(repo), AutoGate(), RunOptions(size="S")
    )
    orch_out = asyncio.run(orch.run("do it"))
    assert orch_out.status == "failed"
    totals = orch.run_state.state["totals"]
    assert totals["cost_usd"] == pytest.approx(0.018)
    assert totals["input_tokens"] >= 7 and totals["num_turns"] >= 5
    assert "total est. cost: $0.02 (subscription login)" in orch_out.report_text


def test_is_error_stage_cost_counted_in_totals(repo):
    err = StageResult(subtype="error_max_turns", is_error=True, cost_usd=0.008)
    orch, _, _ = make(repo, {"implement": [impl()], "test": [err]}, size="S")
    out = go(orch)
    assert out.status == "failed"
    assert orch.run_state.state["totals"]["cost_usd"] == pytest.approx(0.008)
