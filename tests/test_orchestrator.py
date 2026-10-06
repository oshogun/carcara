import asyncio
import json
import os
import subprocess
import tempfile

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
        assert req.env == {"CARCARA_STAGE": req.role or "main"}


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
    assert (
        '"issue":"bug"' in fix_prompt
        and "style" not in fix_prompt.split("## Changes since base")[0]
    )


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


def test_budget_persists_across_resume_unless_overridden(repo):
    orch, _, _ = make(
        repo,
        {"implement": [impl()], "test": [TEST_OK]},
        costs={"implement": 1.0},
        size="S",
        max_budget_usd=1.0,
    )
    out = go(orch)
    assert out.status == "budget_exceeded"
    run_id = out.run_id
    assert RunStore(repo).load(run_id).state["max_budget_usd"] == 1.0
    # Resume without the flag keeps the stored cap: no stage runs.
    orch2, backend2, _ = make(repo, {"test": [TEST_OK]})
    out2 = asyncio.run(orch2.resume(run_id))
    assert (out2.status, out2.exit_code) == ("budget_exceeded", 5)
    assert backend2.requests == []
    assert RunStore(repo).load(run_id).state["max_budget_usd"] == 1.0
    # An explicit higher cap is used and stored.
    orch3, backend3, _ = make(repo, {"test": [TEST_OK]}, max_budget_usd=5.0)
    assert asyncio.run(orch3.resume(run_id)).status == "done"
    assert [r.max_budget_usd for r in backend3.requests] == [4.0]
    assert RunStore(repo).load(run_id).state["max_budget_usd"] == 5.0


def test_budget_none_stored_for_uncapped_run(repo):
    orch, _, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]}, size="S")
    assert go(orch).status == "done"
    assert orch.run_state.state["max_budget_usd"] is None


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


def test_lock_acquire_release_and_ownership(repo):
    from carcara.runstore import RunBusy

    store = RunStore(repo)
    assert store.active() is None
    store.acquire_lock("r1")
    assert store.active()["run_id"] == "r1"
    with pytest.raises(RunBusy) as info:
        store.acquire_lock("r2")
    assert info.value.run_id == "r1"
    store.release_lock("r2")  # not the owner: no-op
    assert store.active()["run_id"] == "r1"
    store.release_lock("r1")
    assert store.active() is None and not store.lock_path.exists()
    assert list(store.base.glob(".active.json.*")) == []


def test_resume_releases_lock(repo):
    orch, _, _ = make(
        repo, {"explore": [EXPLORE], "plan": [PLAN]}, gate=AutoGate(decision="defer"), size="L"
    )
    assert go(orch).status == "awaiting_approval"
    assert not orch.store.lock_path.exists()
    orch2, _, _ = make(repo, {}, gate=AutoGate(decision="reject"), size="L")
    assert asyncio.run(orch2.resume(orch.run_state.id)).status == "failed"
    assert not orch2.store.lock_path.exists()


PLAN_R1 = {**PLAN, "steps": [{"id": "step-r1", "files": ["c.py"], "change": "revised"}]}


@pytest.mark.parametrize("size", ["L", "M"])
def test_reject_with_feedback_replans_then_approve(repo, size):
    orch, _, _ = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        gate=AutoGate(decision="defer"),
        size=size,
        approve_plan=True,
    )
    run_id = go(orch).run_id

    # No explore entry: a re-call would fail loudly.
    orch2, backend2, gate2 = make(repo, {"plan": [PLAN_R1]}, gate=AutoGate(decision="defer"))
    out2 = asyncio.run(orch2.resume(run_id, reject=True, feedback="use c.py instead"))
    assert (out2.status, out2.exit_code) == ("awaiting_approval", 3)
    base = "architect" if size == "L" else "plan"
    assert seq(backend2) == [("plan", "architect" if size == "L" else None)]
    prompt = backend2.requests[0].prompt
    assert "use c.py instead" in prompt and '"change":"one"' in prompt
    assert keys(orch2) == ["explore", base, f"{base}:r1"]
    assert gate2.plans == [PLAN_R1]
    state = orch2.run_state.state
    assert (state["plan_revision"], state["plan_feedback"]) == (1, ["use c.py instead"])

    # Approving replays explore/plan revisions and implements the revised steps.
    orch3, backend3, gate3 = make(
        repo, {"implement": [impl("c.py")], "test": [TEST_OK], "review": [REVIEW_OK]}
    )
    assert asyncio.run(orch3.resume(run_id)).status == "done"
    assert gate3.plans == [PLAN_R1]
    assert [s for s, _ in seq(backend3)] == ["implement", "test", "review"]
    if size == "L":
        assert "implement:step-r1" in keys(orch3)
    assert '"step-r1"' in backend3.requests[0].prompt


def test_reject_without_feedback_fails(repo):
    orch, _, _ = make(
        repo, {"explore": [EXPLORE], "plan": [PLAN]}, gate=AutoGate(decision="defer"), size="L"
    )
    run_id = go(orch).run_id
    orch2, backend2, _ = make(repo, {})
    out = asyncio.run(orch2.resume(run_id, reject=True))
    assert (out.status, out.exit_code) == ("failed", 1)
    assert "plan rejected by user" in out.report_text
    assert backend2.requests == []


def test_guided_retry_after_needs_human(repo):
    orch, _, _ = make(
        repo, {"implement": [impl(), impl(), impl()], "test": [TEST_FAIL] * 3}, size="S"
    )
    run_id = go(orch).run_id
    orch2, backend2, _ = make(repo, {"implement": [impl("b.py")], "test": [TEST_OK]})
    out = asyncio.run(orch2.resume(run_id, feedback="mock the clock"))
    assert (out.status, out.exit_code) == ("done", 0)
    assert seq(backend2) == [("implement", "implementer"), ("test", "test-runner")]
    assert keys(orch2)[-2:] == ["retry-1:guided-implement", "retry-1:test"]
    prompt = backend2.requests[0].prompt
    assert "mock the clock" in prompt and '"name":"t1"' in prompt


def test_accept_failures_without_backend_calls(repo):
    orch, _, _ = make(
        repo, {"implement": [impl(), impl(), impl()], "test": [TEST_FAIL] * 3}, size="S"
    )
    run_id = go(orch).run_id
    orch2, backend2, _ = make(repo, {})
    out = asyncio.run(orch2.resume(run_id, accept_failures=True))
    assert (out.status, out.exit_code) == ("done", 0)
    assert backend2.requests == []
    assert orch2.run_state.state["accepted_failures"] is True
    assert "accepted failures" in out.report_text


@pytest.mark.parametrize(
    "flags",
    [
        {"reject": True},
        {"accept_failures": True},
        {"feedback": "x", "accept_failures": True},
    ],
)
def test_resume_flags_invalid_for_status(repo, flags):
    orch, _, _ = make(
        repo, {"explore": [EXPLORE], "plan": [PLAN]}, gate=AutoGate(decision="defer"), size="L"
    )
    run_id = go(orch).run_id
    if flags.get("reject"):
        # A done run cannot be rejected.
        orch_s, _, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]}, size="S")
        run_id = go(orch_s).run_id
    orch2, backend2, _ = make(repo, {})
    with pytest.raises(OrchestratorError):
        asyncio.run(orch2.resume(run_id, **flags))
    assert backend2.requests == []


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


class EditingBackend(FakeBackend):
    """FakeBackend whose implement stages write files like a real implementer."""

    def __init__(self, script, repo, edits):
        super().__init__(script)
        self.repo = repo
        self.edits = list(edits)

    async def run_stage(self, request):
        if request.stage == "implement" and self.edits:
            for name, text in self.edits.pop(0).items():
                (self.repo / name).write_text(text)
        return await super().run_stage(request)


def make_editing(repo, script, edits, gate=None, **opts):
    backend = EditingBackend(script, repo, edits)
    gate = gate or AutoGate()
    orch = Orchestrator(backend, PROFILE, str(repo), RunStore(repo), gate, RunOptions(**opts))
    return orch, backend


def review_prompt(backend):
    return next(r.prompt for r in backend.requests if r.stage == "review")


def test_dirty_repo_snapshot_base_shows_only_carcara_edits(repo):
    (repo / ".env").write_text("TRACKED_SECRET=1\n")
    git(repo, "add", ".env")
    git(repo, "commit", "-q", "-m", "env")
    index_before = git(repo, "ls-files", "-s")
    (repo / "a.py").write_text("x = 'user edit'\n")
    (repo / "notes.txt").write_text("pre-existing untracked\n")
    (repo / ".env").write_text("TRACKED_SECRET=2\n")
    (repo / "sub").mkdir()
    (repo / "sub" / ".env").write_text("UNTRACKED_SECRET=3\n")
    tmp_before = set(os.listdir(tempfile.gettempdir()))

    orch, backend = make_editing(
        repo,
        {"implement": [impl("b.py")], "test": [TEST_OK], "review": [REVIEW_OK]},
        [{"b.py": "carcara_was_here = 1\n", ".env": "TRACKED_SECRET=carcara\n"}],
        size="S",
        review_small=True,
        allow_dirty=True,
    )
    out = go(orch)
    assert out.status == "done"
    state = orch.run_state.state
    assert state["base_kind"] == "snapshot"
    assert git(repo, "rev-parse", f"refs/carcara/{out.run_id}") == state["base_sha"]
    assert git(repo, "rev-parse", f"{state['base_sha']}^") == git(repo, "rev-parse", "HEAD")
    assert f"carcara changes: carcara diff {out.run_id}" in out.report_text
    # The tracked secret keeps its committed blob in the snapshot (not dropped,
    # not the working copy), so a plain `git diff <base>` never shows it as new.
    assert git(repo, "rev-parse", f"{state['base_sha']}:.env") == git(
        repo, "rev-parse", "HEAD:.env"
    )
    assert ".env" not in git(repo, "diff", "--name-only", "HEAD", state["base_sha"])

    prompt = review_prompt(backend)
    diff = prompt.split("## Changes since base", 1)[1]
    assert "carcara_was_here" in diff and "b.py" in diff
    assert "user edit" not in diff and "a.py" not in diff
    assert "notes.txt" not in diff
    assert "SECRET" not in prompt and ".env" not in diff
    # The user's index is untouched and the temp index is cleaned up.
    assert git(repo, "ls-files", "-s") == index_before
    leftovers = set(os.listdir(tempfile.gettempdir())) - tmp_before
    assert not [n for n in leftovers if n.startswith("carcara-index-")]


def test_clean_repo_head_base_hides_tracked_secret(repo):
    (repo / ".env").write_text("TRACKED_SECRET=1\n")
    git(repo, "add", ".env")
    git(repo, "commit", "-q", "-m", "env")
    orch, backend = make_editing(
        repo,
        {"implement": [impl("b.py")], "test": [TEST_OK], "review": [REVIEW_OK]},
        [{"b.py": "carcara_was_here = 1\n"}],
        size="S",
        review_small=True,
    )
    out = go(orch)
    assert out.status == "done"
    state = orch.run_state.state
    head = git(repo, "rev-parse", "HEAD")
    assert (state["base_sha"], state["base_kind"]) == (head, "head")
    assert git(repo, "rev-parse", f"refs/carcara/{out.run_id}") == head
    prompt = review_prompt(backend)
    assert "carcara_was_here" in prompt
    assert "SECRET" not in prompt and ".env" not in prompt.split("## Changes since base", 1)[1]


def test_allow_dirty_with_clean_tree_uses_head(repo):
    orch, _, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]}, size="S", allow_dirty=True)
    assert go(orch).status == "done"
    assert orch.run_state.state["base_kind"] == "head"


def test_gate_resume_resnapshots_base(repo):
    orch, _ = make_editing(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        [],
        gate=AutoGate(decision="defer"),
        size="M",
        approve_plan=True,
    )
    out = go(orch)
    assert out.status == "awaiting_approval"
    first_base = orch.run_state.state["base_sha"]
    (repo / "a.py").write_text("x = 'edited while reviewing'\n")

    orch2, backend2 = make_editing(
        repo,
        {"implement": [impl("b.py")], "test": [TEST_OK], "review": [REVIEW_OK]},
        [{"b.py": "carcara_was_here = 1\n"}],
    )
    out2 = asyncio.run(orch2.resume(out.run_id))
    assert out2.status == "done"
    state = orch2.run_state.state
    assert state["base_kind"] == "snapshot" and state["base_sha"] != first_base
    assert git(repo, "rev-parse", f"refs/carcara/{out.run_id}") == state["base_sha"]
    diff = review_prompt(backend2).split("## Changes since base", 1)[1]
    assert "carcara_was_here" in diff
    assert "edited while reviewing" not in diff


def test_needs_human_resume_keeps_base(repo):
    orch, _ = make_editing(
        repo,
        {"implement": [impl(), impl(), impl()], "test": [TEST_FAIL] * 3},
        [{"b.py": "carcara_was_here = 1\n"}],
        size="S",
    )
    out = go(orch)
    assert out.status == "needs_human"
    base = orch.run_state.state["base_sha"]
    (repo / "a.py").write_text("x = 'human fix'\n")
    orch2, backend2 = make_editing(
        repo, {"test": [TEST_OK], "review": [REVIEW_OK]}, [], review_small=True
    )
    out2 = asyncio.run(orch2.resume(out.run_id))
    assert out2.status == "done"
    assert orch2.run_state.state["base_sha"] == base
    assert git(repo, "rev-parse", f"refs/carcara/{out.run_id}") == base
    diff = review_prompt(backend2).split("## Changes since base", 1)[1]
    assert "carcara_was_here" in diff and "human fix" in diff


def test_blocked_implement_resume_with_feedback_reruns_with_guidance(repo):
    orch, _, _ = make(repo, {"implement": [impl(blocked=True)]}, size="S")
    out = go(orch)
    assert out.status == "needs_human"
    assert orch.run_state.state["blocked"]["key"] == "implement"
    orch2, backend2, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]})
    out2 = asyncio.run(orch2.resume(out.run_id, feedback="use the v2 api"))
    assert out2.status == "done"
    # No extra guided-implement: the feedback goes to the re-run implement.
    assert seq(backend2) == [("implement", "implementer"), ("test", "test-runner")]
    prompt = backend2.requests[0].prompt
    assert "use the v2 api" in prompt and "blocked on x" in prompt
    assert "implement" in keys(orch2)
    assert "retry-1:guided-implement" not in keys(orch2)


def test_blocked_fix_implement_feedback_goes_to_guided_implement(repo):
    orch, _, _ = make(
        repo, {"implement": [impl(), impl(blocked=True)], "test": [TEST_FAIL]}, size="S"
    )
    out = go(orch)
    assert out.status == "needs_human"
    assert orch.run_state.state["blocked"]["key"] == "fix-1:implement"
    orch2, backend2, _ = make(repo, {"implement": [impl()], "test": [TEST_OK]})
    out2 = asyncio.run(orch2.resume(out.run_id, feedback="use the v2 api"))
    assert out2.status == "done"
    assert keys(orch2)[-2:] == ["retry-1:guided-implement", "retry-1:test"]
    assert "use the v2 api" in backend2.requests[0].prompt


def test_run_diff_matches_review_diff_and_hides_secrets(repo):
    from carcara.orchestrator import run_diff

    (repo / ".env").write_text("TRACKED_SECRET=1\n")
    git(repo, "add", ".env")
    git(repo, "commit", "-q", "-m", "env")
    (repo / ".env").write_text("TRACKED_SECRET=2\n")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "b.py").write_text("y = " + "1" * 30000 + "\n")
    diff = run_diff(str(repo), head)
    assert "b.py" in diff and "1" * 30000 in diff  # not truncated
    assert "SECRET" not in diff and ".env" not in diff
    stat = run_diff(str(repo), head, stat=True)
    assert "b.py" in stat and ".env" not in stat


def test_run_refuses_non_toplevel_cwd(repo):
    (repo / "sub").mkdir()
    orch = Orchestrator(
        FakeBackend({}), PROFILE, str(repo / "sub"), RunStore(repo / "sub"), AutoGate()
    )
    with pytest.raises(OrchestratorError, match=r"^run from the repository root \("):
        go(orch)
    with pytest.raises(OrchestratorError, match="repository root"):
        asyncio.run(orch.resume("anything"))
    assert not (repo / "sub" / ".carcara").exists()


def test_resume_revalidates_state_after_taking_lock(repo, monkeypatch):
    orch, _, _ = make(
        repo, {"explore": [EXPLORE], "plan": [PLAN]}, gate=AutoGate(decision="defer"), size="L"
    )
    assert go(orch).status == "awaiting_approval"
    run_id = orch.run_state.id
    store = RunStore(repo)
    real_acquire = store.acquire_lock

    def racing_acquire(rid):
        # Another process finished the run between our check and the lock.
        run = store.load(rid)
        run.state["status"] = "done"
        run.save()
        real_acquire(rid)

    monkeypatch.setattr(store, "acquire_lock", racing_acquire)
    backend = FakeBackend({})
    orch2 = Orchestrator(backend, PROFILE, str(repo), store, AutoGate(), RunOptions(size="L"))
    with pytest.raises(OrchestratorError, match="--reject needs a run awaiting approval"):
        asyncio.run(orch2.resume(run_id, reject=True))
    assert not store.lock_path.exists()
    out = asyncio.run(orch2.resume(run_id))
    assert out.status == "done" and backend.requests == []
    assert not store.lock_path.exists()
