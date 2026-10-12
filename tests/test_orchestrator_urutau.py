"""Orchestrator ↔ Urutau record_run reporting (fake transport, no network)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_orchestrator import (
    EXPLORE,
    PLAN,
    PROFILE,
    REVIEW_MAJOR,
    REVIEW_OK,
    TEST_OK,
    TRIAGE_S,
    _refute,
    impl,
    ordered_run_ids,  # noqa: F401 - pytest fixture
)

from carcara.backend import FakeBackend
from carcara.orchestrator import MAX_FINDINGS_CHARS, AutoGate, Orchestrator, RunOptions
from carcara.runstore import RunStore
from carcara.urutau import ToolReply, UrutauClient, UrutauConfig, UrutauError

FIX = Path(__file__).parent / "fixtures" / "urutau"
TOKEN = "tok-SECRET-123"
# §18 input fields.
ALLOWED = {
    "repo",
    "issue",
    "runId",
    "status",
    "triageRange",
    "uncertaintyKind",
    "unverified",
    "mergeShas",
    "files",
    "areas",
    "observedBy",
    "fixRounds",
    "filesOmitted",
    "withdrawn",
    "costUsd",
    "findings",
    "probes",
}
INVENTORY_KEYS = {"unverified", "withdrawn", "probes"}


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


def sample(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())


def ok(data: dict[str, Any]) -> ToolReply:
    return ToolReply(is_error=False, data=data, text=json.dumps(data))


def err(data: dict[str, Any]) -> ToolReply:
    return ToolReply(is_error=True, data=data, text=json.dumps(data))


class FakeTransport:
    def __init__(self, log: list[tuple[str, dict]], respond):
        self.log, self.respond = log, respond

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolReply:
        self.log.append((name, json.loads(json.dumps(args))))
        return self.respond(name, args)

    async def aclose(self) -> None:
        pass


def default_respond(name: str, args: dict[str, Any]) -> ToolReply:
    if name == "get_board":
        return ok({"repo": "acme/widgets", "buckets": []})
    return ok(sample("record_run-start.output.json"))


def client(log, respond=default_respond) -> UrutauClient:
    async def sleep(s: float) -> None:
        pass

    return UrutauClient(
        UrutauConfig("http://127.0.0.1:8787", TOKEN),
        "acme/widgets",
        7,
        transport_factory=lambda cfg: FakeTransport(log, respond),
        sleep=sleep,
    )


def make(repo, script, *, log, respond=default_respond, gate=None, backend=None, **opts):
    backend = backend or FakeBackend(script)
    orch = Orchestrator(
        backend,
        PROFILE,
        str(repo),
        RunStore(repo),
        gate or AutoGate(),
        RunOptions(issue=7, repo="acme/widgets", **opts),
        urutau=client(log, respond),
    )
    return orch, backend


def record_calls(log) -> list[dict[str, Any]]:
    return [args for name, args in log if name == "record_run"]


def run_files(orch) -> str:
    d = orch.run_state.dir
    return "".join((d / n).read_text() for n in ("state.json", "report.md", "events.jsonl"))


S_SCRIPT = {"triage": [TRIAGE_S], "implement": [impl()], "test": [TEST_OK]}


def test_done_run_calls_start_then_terminal(repo):
    log: list = []
    orch, backend = make(repo, S_SCRIPT, log=log)
    out = asyncio.run(orch.run("do it"))
    assert (out.status, out.exit_code) == ("done", 0)
    assert [n for n, _ in log] == ["get_board", "record_run", "record_run"]
    start, end = record_calls(log)
    assert start["status"] == "running" and not INVENTORY_KEYS & start.keys()
    assert end["status"] == "done"
    for call in (start, end):
        assert set(call) <= ALLOWED
        assert call["runId"] == out.run_id
        assert (call["repo"], call["issue"]) == ("acme/widgets", 7)
        assert call["observedBy"] == "carcara/extent-1"
    assert (end["triageRange"], end["uncertaintyKind"]) == ("S", "none")
    assert end["fixRounds"] == 0 and end["costUsd"] == 0.0
    assert end["files"] == ["a.py"] and end["filesOmitted"] == 0
    state = orch.run_state.state
    assert state["urutau"]["enabled"] and state["urutau"]["last"]["ok"]
    assert (state["triage_range"], state["uncertainty_kind"]) == ("S", "none")
    assert "Urutau reporting: on" in out.report_text
    assert "Triage range: S; uncertainty: none" in out.report_text
    assert "Issue: #7 (acme/widgets); card estimate: none" in out.report_text
    assert TOKEN not in run_files(orch)
    for req in backend.requests:
        assert TOKEN not in req.prompt and TOKEN not in json.dumps(req.env)


def test_claim_conflict_stops_before_any_stage(repo):
    log: list = []

    def respond(name, args):
        if name == "record_run":
            return err(sample("record_run-claimed-by-other-run.error.json"))
        return default_respond(name, args)

    orch, backend = make(repo, S_SCRIPT, log=log, respond=respond)
    out = asyncio.run(orch.run("do it"))
    assert (out.status, out.exit_code) == ("failed", 1)
    assert "claimed by another run" in out.report_text
    assert backend.requests == [] and orch.run_state.state["stages"] == []


def test_heartbeat_sends_running_without_inventory(repo):
    class SlowBackend(FakeBackend):
        async def run_stage(self, request):
            await asyncio.sleep(0.05)
            return await super().run_stage(request)

    log: list = []
    orch, _ = make(repo, {}, log=log, backend=SlowBackend(S_SCRIPT))
    orch.heartbeat_interval = 0.01
    assert asyncio.run(orch.run("do it")).status == "done"
    calls = record_calls(log)
    beats = calls[1:-1]
    assert beats and all(c["status"] == "running" for c in beats)
    assert all(not INVENTORY_KEYS & c.keys() and "findings" not in c for c in beats)
    assert calls[-1]["status"] == "done"


def test_terminal_failure_only_warns(repo):
    log: list = []

    def respond(name, args):
        if name == "record_run" and args["status"] == "done":
            raise UrutauError("transport", "down")
        return default_respond(name, args)

    orch, _ = make(repo, S_SCRIPT, log=log, respond=respond)
    out = asyncio.run(orch.run("do it"))
    assert (out.status, out.exit_code) == ("done", 0)
    events = (orch.run_state.dir / "events.jsonl").read_text()
    assert '"source":"urutau"' in events.replace(" ", "")
    assert orch.run_state.state["urutau"]["last"]["ok"] is False


def test_plan_rejection_sends_rejected(repo):
    log: list = []
    orch, _ = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        log=log,
        gate=AutoGate(approve=False),
        size="L",
    )
    out = asyncio.run(orch.run("do it"))
    assert out.status == "failed"
    assert record_calls(log)[-1]["status"] == "rejected"
    # Forced size: no triage range or uncertainty kind is sent.
    assert not {"triageRange", "uncertaintyKind"} & record_calls(log)[-1].keys()


def test_plan_only_sends_plan_goal(repo):
    log: list = []
    orch, _ = make(repo, {"explore": [EXPLORE], "plan": [PLAN]}, log=log, size="L", plan_only=True)
    assert asyncio.run(orch.run("do it")).status == "plan_only"
    last = record_calls(log)[-1]
    assert last["status"] == "plan_only" and last["findings"].startswith("Plan: g")


def terminal_respond(name, args):
    if name == "record_run" and args["status"] != "running":
        return ok(sample("record_run-done-with-probe.output.json"))
    return default_respond(name, args)


def test_triage_only_claims_then_reports_plan_only(repo):
    log: list = []
    orch, backend = make(
        repo, {"triage": [TRIAGE_S]}, log=log, respond=terminal_respond, triage_only=True
    )
    out = asyncio.run(orch.run("do it"))
    assert (out.status, out.exit_code) == ("triaged", 0)
    assert len(backend.requests) == 1
    calls = record_calls(log)
    assert [c["status"] for c in calls] == ["running", "plan_only"]
    last = calls[-1]
    assert set(last) <= ALLOWED
    assert (last["triageRange"], last["uncertaintyKind"]) == ("S", "none")
    assert last["findings"] == "Triage: size S, range S, uncertainty none. small"
    urutau = orch.run_state.state["urutau"]["last"]
    assert urutau["status"] == "plan_only" and urutau["claim_held"] is False


def test_triage_only_findings_truncated_and_never_empty(repo):
    log: list = []
    long = {**TRIAGE_S, "rationale": "x" * 9000}
    orch, _ = make(repo, {"triage": [long]}, log=log, triage_only=True)
    assert asyncio.run(orch.run("do it")).status == "triaged"
    findings = record_calls(log)[-1]["findings"]
    assert findings.startswith("Triage:") and len(findings) == MAX_FINDINGS_CHARS

    log2: list = []
    bare = {**TRIAGE_S, "rationale": "  "}
    orch2, _ = make(repo, {"triage": [bare]}, log=log2, triage_only=True)
    assert asyncio.run(orch2.run("again")).status == "triaged"
    assert record_calls(log2)[-1]["findings"] == "Triage: size S, range S, uncertainty none."


def test_triage_only_claim_conflict_runs_no_stage(repo):
    log: list = []

    def respond(name, args):
        if name == "record_run":
            return err(sample("record_run-claimed-by-other-run.error.json"))
        return default_respond(name, args)

    orch, backend = make(repo, {"triage": [TRIAGE_S]}, log=log, respond=respond, triage_only=True)
    out = asyncio.run(orch.run("do it"))
    assert (out.status, out.exit_code) == ("failed", 1)
    assert backend.requests == []


def test_run_after_triaged_run_gets_new_run_id(repo):
    log: list = []
    orch, _ = make(repo, {"triage": [TRIAGE_S]}, log=log, triage_only=True)
    first = asyncio.run(orch.run("do it"))
    log2: list = []
    orch2, _ = make(repo, S_SCRIPT, log=log2)
    second = asyncio.run(orch2.run("do it"))
    assert second.status == "done"
    assert {c["runId"] for c in record_calls(log)} == {first.run_id}
    assert {c["runId"] for c in record_calls(log2)} == {second.run_id}
    assert first.run_id != second.run_id


def test_pause_and_resume(repo):
    log: list = []
    orch, _ = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        log=log,
        gate=AutoGate(decision="defer"),
        size="L",
    )
    out = asyncio.run(orch.run("do it"))
    assert out.status == "awaiting_approval"
    assert [c["status"] for c in record_calls(log)] == ["running", "awaiting_approval"]

    log2: list = []
    orch2, _ = make(
        repo,
        {"implement": [impl(), impl()], "test": [TEST_OK], "review": [REVIEW_OK]},
        log=log2,
    )
    out2 = asyncio.run(orch2.resume(out.run_id))
    assert out2.status == "done"
    assert [c["status"] for c in record_calls(log2)] == ["running", "done"]


def test_resume_claim_conflict_stops_before_drive(repo):
    log: list = []
    orch, _ = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        log=log,
        gate=AutoGate(decision="defer"),
        size="L",
    )
    run_id = asyncio.run(orch.run("do it")).run_id

    def respond(name, args):
        return err(sample("record_run-claimed-by-other-run.error.json"))

    orch2, backend2 = make(repo, {}, log=[], respond=respond)
    out = asyncio.run(orch2.resume(run_id))
    assert (out.status, out.exit_code) == ("failed", 1)
    assert backend2.requests == []


def test_without_client_nothing_is_sent(repo):
    orch = Orchestrator(
        FakeBackend(S_SCRIPT),
        PROFILE,
        str(repo),
        RunStore(repo),
        AutoGate(),
        RunOptions(issue=7, repo="acme/widgets"),
    )
    out = asyncio.run(orch.run("do it"))
    assert out.status == "done"
    state = orch.run_state.state
    assert state["urutau"]["enabled"] is False and state["issue"] == 7
    assert "Urutau reporting: off" in out.report_text


class SlowBackend(FakeBackend):
    async def run_stage(self, request):
        await asyncio.sleep(0.05)
        return await super().run_stage(request)


def test_heartbeat_failure_in_task_group_still_finishes(repo):
    anyio = pytest.importorskip("anyio")
    log: list = []

    class TaskGroupTransport(FakeTransport):
        """Like the MCP SDK: each call runs inside its own anyio task group."""

        async def call_tool(self, name, args):
            async with anyio.create_task_group() as tg:
                tg.start_soon(anyio.sleep, 10)
                reply = await super().call_tool(name, args)
                beats = [a for n, a in self.log if n == "record_run"]
                if args.get("status") == "running" and len(beats) > 1:
                    raise RuntimeError("heartbeat transport broke")
                tg.cancel_scope.cancel()
            return reply

    orch, _ = make(repo, {}, log=log, backend=SlowBackend(S_SCRIPT))
    orch.urutau._factory = lambda cfg: TaskGroupTransport(log, default_respond)
    orch.heartbeat_interval = 0.01
    out = asyncio.run(orch.run("do it"))
    assert (out.status, out.exit_code) == ("done", 0)
    assert (orch.run_state.dir / "report.md").exists()
    assert orch.run_state.state["status"] == "done"
    calls = record_calls(log)
    assert len(calls) > 2 and calls[-1]["status"] == "done"
    last = orch.run_state.state["urutau"]["last"]
    assert (last["status"], last["ok"]) == ("done", True)
    events = (orch.run_state.dir / "events.jsonl").read_text().splitlines()
    assert any(
        e["event"] == "warning" and e.get("source") == "urutau" for e in map(json.loads, events)
    )


@pytest.mark.skipif(not hasattr(asyncio.Task, "cancelling"), reason="needs Task.cancelling (3.11+)")
def test_stray_cancel_on_terminal_send_keeps_outcome(repo):
    log: list = []

    def respond(name, args):
        if name == "record_run" and args["status"] == "done":
            raise asyncio.CancelledError
        return default_respond(name, args)

    orch, _ = make(repo, S_SCRIPT, log=log, respond=respond)
    out = asyncio.run(orch.run("do it"))
    assert (out.status, out.exit_code) == ("done", 0)
    assert (orch.run_state.dir / "report.md").exists()
    last = orch.run_state.state["urutau"]["last"]
    assert (last["status"], last["ok"], last["code"]) == ("done", False, "cancelled")


def test_resume_failed_issue_run_mints_r2(repo):
    log: list = []
    orch, _ = make(repo, {"triage": [TRIAGE_S], "implement": [impl()]}, log=log)
    out = asyncio.run(orch.run("do it"))
    assert out.status == "failed"
    assert record_calls(log)[-1]["status"] == "failed"
    state = orch.run_state.state
    assert state["urutau"]["run_id"] == out.run_id
    state["urutau"]["sent_items"] = {"U1": {"local_id": "U1", "kind": "external", "text": "t"}}
    orch.run_state.save()

    log2: list = []
    orch2, _ = make(repo, {"test": [TEST_OK]}, log=log2)
    out2 = asyncio.run(orch2.resume(out.run_id))
    assert out2.status == "done"
    calls = record_calls(log2)
    assert [c["status"] for c in calls] == ["running", "done"]
    assert {c["runId"] for c in calls} == {f"{out.run_id}-r2"}
    urutau_state = orch2.run_state.state["urutau"]
    assert urutau_state["run_id"] == f"{out.run_id}-r2" and urutau_state["sent_items"] == {}


def test_resume_mints_new_id_when_old_one_finished(repo):
    """An unconfirmed terminal call that landed: running gets run-finished, so mint once."""
    log: list = []
    orch, _ = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        log=log,
        gate=AutoGate(decision="defer"),
        size="L",
    )
    out = asyncio.run(orch.run("do it"))
    finished = sample("record_run-run-finished.error.json")

    def respond(name, args):
        if name == "record_run" and args["runId"] == out.run_id:
            return err(finished)
        return default_respond(name, args)

    log2: list = []
    orch2, _ = make(
        repo,
        {"implement": [impl(), impl()], "test": [TEST_OK], "review": [REVIEW_OK]},
        log=log2,
        respond=respond,
    )
    assert asyncio.run(orch2.resume(out.run_id)).status == "done"
    ids = [c["runId"] for c in record_calls(log2)]
    assert ids == [out.run_id, f"{out.run_id}-r2", f"{out.run_id}-r2"]


def test_resume_clears_plan_rejected(repo):
    log: list = []
    orch, _ = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        log=log,
        gate=AutoGate(approve=False),
        size="L",
    )
    out = asyncio.run(orch.run("do it"))
    assert record_calls(log)[-1]["status"] == "rejected"
    assert orch.run_state.state["plan_rejected"] is True

    # Approved this time, then a stage fails: that is a failure, not a rejection.
    log2: list = []
    orch2, _ = make(repo, {"implement": [impl()]}, log=log2, size="L")
    out2 = asyncio.run(orch2.resume(out.run_id))
    assert out2.status == "failed"
    assert orch2.run_state.state["plan_rejected"] is False
    assert [c["status"] for c in record_calls(log2)] == ["running", "failed"]
    assert record_calls(log2)[0]["runId"] == f"{out.run_id}-r2"


def test_interactive_gate_sends_awaiting_approval_first(repo):
    log: list = []

    class PromptGate(AutoGate):
        interactive = True

        def __init__(self):
            super().__init__()
            self.seen: list[list[str]] = []

        def approve_plan(self, plan):
            self.seen.append([c["status"] for c in record_calls(log)])
            return super().approve_plan(plan)

    gate = PromptGate()
    orch, _ = make(
        repo,
        {
            "explore": [EXPLORE],
            "plan": [PLAN],
            "implement": [impl(), impl()],
            "test": [TEST_OK],
            "review": [REVIEW_OK],
        },
        log=log,
        gate=gate,
        size="L",
    )
    assert asyncio.run(orch.run("do it")).status == "done"
    assert gate.seen == [["running", "awaiting_approval"]]
    assert [c["status"] for c in record_calls(log)] == [
        "running",
        "awaiting_approval",
        "running",
        "done",
    ]


def test_plan_only_findings_include_steps_and_acceptance(repo):
    log: list = []
    orch, _ = make(repo, {"explore": [EXPLORE], "plan": [PLAN]}, log=log, size="L", plan_only=True)
    assert asyncio.run(orch.run("do it")).status == "plan_only"
    findings = record_calls(log)[-1]["findings"]
    assert findings == "Plan: g\nSteps:\n- step-1: one\n- step-2: two\nAcceptance:\n- a"

    big = {**PLAN, "goal": "x" * 3000, "acceptance": ["y" * 3000]}
    log2: list = []
    orch2, _ = make(repo, {"explore": [EXPLORE], "plan": [big]}, log=log2, size="L", plan_only=True)
    assert asyncio.run(orch2.run("again")).status == "plan_only"
    assert len(record_calls(log2)[-1]["findings"]) == 4000


def _with_inventory(review, *texts):
    items = [{"id": f"U{n}", "kind": "untested", "text": t} for n, t in enumerate(texts, 1)]
    return {**review, "unverified": items}


def test_ultra_inventory_comes_only_from_merge_review(repo):
    merge = [
        _with_inventory(REVIEW_MAJOR, "merge item"),
        _with_inventory(REVIEW_OK, "merge item"),
    ]
    common = {
        "plan": [PLAN],
        "implement": [impl(), impl()],
        "test": [TEST_OK, TEST_OK],
        "review": merge,
    }
    # A reasoned major, so no reason follow-up runs.
    reasoned = [{**f, "reason": "correctness"} for f in REVIEW_MAJOR["findings"][:1]]
    dim = _with_inventory({**REVIEW_MAJOR, "findings": reasoned}, "dim only", "merge item")
    ultra = {
        **common,
        "scope": [
            {"areas": [{"id": a, "focus": a} for a in ("a", "b")], "rationale": "r"},
        ],
        # The explore schema has no `unverified`; only review-dim can carry stray items.
        "explore:a": [EXPLORE],
        "explore:b": [EXPLORE],
        "review-dim": [dim] * 3 + [_with_inventory(REVIEW_OK, "dim only")] * 3,
        "review-refute": [_refute()],
    }
    log: list = []
    orch, _ = make(repo, ultra, log=log, size="M", ultra=True)
    assert asyncio.run(orch.run("do it")).status == "done"
    end = record_calls(log)[-1]

    plain_log: list = []
    plain, _ = make(repo, {**common, "explore": [EXPLORE]}, log=plain_log, size="M")
    assert asyncio.run(plain.run("do it again")).status == "done"
    plain_end = record_calls(plain_log)[-1]

    assert [i["text"] for i in end["unverified"]] == ["merge item"]
    assert end["unverified"] == plain_end["unverified"]
    assert end["fixRounds"] == plain_end["fixRounds"] == 1
    assert end["areas"] == plain_end["areas"]
    assert end["files"] == plain_end["files"]
    assert orch.run_state.state["unverified_reviews"] == ["review", "fix-1:review"]


CARRY_REVIEW = {
    **REVIEW_OK,
    "unverified": [
        {"id": "U1", "kind": "external", "text": "ext item"},
        {"id": "U2", "kind": "normative", "text": "norm item"},
        {"id": "U3", "kind": "untested", "text": "untested item"},
        {"id": "U4", "kind": "external", "text": "probed away"},
    ],
}


def _reviewed_first_run(repo, review=CARRY_REVIEW):
    log: list = []
    script = {"implement": [impl()], "test": [TEST_OK], "review": [review]}
    orch, _ = make(repo, script, log=log, size="S", review_small=True)
    assert asyncio.run(orch.run("do it")).status == "done"
    prev = orch.run_state
    prev.state["unverified"][3]["resolved"] = True
    prev.save()
    return prev


def _open_counts(respond_log):
    def respond(name, args):
        if name == "get_board":
            return ok({"repo": "acme/widgets", "buckets": []})
        data = sample("record_run-start.output.json")
        counts = {"external": 0, "normative": 0, "untested": 0}
        for item in args.get("unverified", []):
            counts[item["kind"]] += 1
        respond_log.append(counts)
        return ok(
            {**data, "runId": args["runId"], "status": args["status"], "unverifiedOpen": counts}
        )

    return respond


def test_fix_round_run_resends_open_items_from_previous_run(repo):
    prev = _reviewed_first_run(repo)
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_open_counts([]))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    start, end = record_calls(log)
    assert not INVENTORY_KEYS & start.keys()
    assert end["runId"] == out.run_id != prev.id
    assert [(i["id"], i["text"]) for i in end["unverified"]] == [
        ("U1", "ext item"),
        ("U2", "norm item"),
        ("U3", "untested item"),
    ]
    last = orch.run_state.state["urutau"]["last"]
    assert last["unverified_open"] == {"external": 1, "normative": 1, "untested": 1}
    assert f"carried from {prev.id}: 3" in out.report_text


def test_review_in_new_run_drops_and_rewords_carried_items(repo):
    _reviewed_first_run(repo)
    second = {
        **REVIEW_OK,
        "unverified": [{"id": "U3", "kind": "untested", "text": "untested item, reworded"}],
    }
    log: list = []
    orch, _ = make(
        repo,
        {"implement": [impl()], "test": [TEST_OK], "review": [second]},
        log=log,
        size="S",
        review_small=True,
    )
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    end = record_calls(log)[-1]
    assert [(i["id"], i["text"]) for i in end["unverified"]] == [
        ("U5", "untested item, reworded"),
    ]
    assert "withdrawn" not in end


def test_carried_items_sent_then_withdrawn_within_new_run(repo):
    _reviewed_first_run(repo)
    second = {**REVIEW_OK, "unverified": [{"id": "N", "kind": "untested", "text": "fresh"}]}
    log: list = []
    orch, _ = make(
        repo,
        {"explore": [EXPLORE], "plan": [PLAN]},
        log=log,
        gate=AutoGate(decision="defer"),
        size="L",
    )
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "awaiting_approval"
    pause = record_calls(log)[-1]
    assert [i["id"] for i in pause["unverified"]] == ["U1", "U2", "U3"]

    log2: list = []
    orch2, _ = make(
        repo,
        {"implement": [impl(), impl()], "test": [TEST_OK], "review": [second]},
        log=log2,
    )
    assert asyncio.run(orch2.resume(out.run_id)).status == "done"
    end = record_calls(log2)[-1]
    assert [i["text"] for i in end["unverified"]] == ["fresh"]
    # Urutau cannot withdraw a normative item already sent in this run; carcara
    # stops listing it, so the next run on the issue does not carry it.
    assert sorted(end["withdrawn"]) == ["U1", "U3"]
    state = orch2.run_state.state
    assert [i["id"] for i in state["unverified"]] == ["U5"]


def test_triage_only_run_sends_carried_items(repo):
    prev = _reviewed_first_run(repo)
    log: list = []
    orch, _ = make(repo, {"triage": [TRIAGE_S]}, log=log, triage_only=True)
    out = asyncio.run(orch.run("triage"))
    end = record_calls(log)[-1]
    assert end["status"] == "plan_only"
    assert [i["id"] for i in end["unverified"]] == ["U1", "U2", "U3"]
    assert {i["carried_from"] for i in orch.run_state.state["unverified"]} == {prev.id}
    assert out.run_id != prev.id


def _card_respond(items, *, board_error=None):
    """get_board shows issue 7's card with ``items`` (None: no item list, an older
    Urutau); record_run as usual. Like Urutau, the items come only with
    ``includeUnverifiedItems: true``."""
    record = _open_counts([])

    def respond(name, args):
        if name != "get_board":
            return record(name, args)
        if board_error is not None:
            return board_error
        card = dict(sample("get_board-card.json"))
        if items is not None and args.get("includeUnverifiedItems") is True:
            card["lastRun"] = {**card["lastRun"], "unverifiedItems": items}
        return ok({"repo": "acme/widgets", "buckets": [{"id": "doing", "cards": [card]}]})

    return respond


def _events(orch) -> list[dict[str, Any]]:
    text = (orch.run_state.dir / "events.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def _sent_ids(log) -> set[str]:
    return {i["id"] for c in record_calls(log) for i in c.get("unverified", [])}


def test_item_resolved_on_card_is_not_carried_or_resent(repo):
    prev = _reviewed_first_run(repo)
    items = [
        {"id": "U1", "kind": "external", "text": "ext item", "status": "resolved"},
        {"id": "U2", "kind": "normative", "text": "norm item", "status": "open"},
    ]
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_card_respond(items))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    # One board read serves both the carry check and the estimate.
    assert [n for n, _ in log] == ["get_board", "record_run", "record_run"]
    assert log[0][1]["includeUnverifiedItems"] is True
    state = orch.run_state.state
    assert [i["id"] for i in state["unverified"]] == ["U2", "U3"]
    assert state["unverified_next"] == 5
    assert "U1" not in _sent_ids(log)
    assert state["urutau"]["carry_check"] == {
        "status": "ok",
        "dropped": 1,
        "resolved": 1,
        "from": prev.id,
    }
    carried = [e for e in _events(orch) if e["event"] == "unverified_carried"]
    assert carried[0]["count"] == 2 and carried[0]["dropped_resolved"] == 1
    assert "dropped 1 item(s) resolved on Urutau" in out.report_text
    assert f"carried from {prev.id}: 2" in out.report_text


def test_item_resolved_on_card_matched_by_sent_id_or_text(repo):
    prev = _reviewed_first_run(repo)
    sent = prev.state["urutau"]["sent_items"]
    sent["U1-r1"] = {**sent.pop("U1"), "withdrawn": False}
    prev.save()
    items = [
        {"id": "U1-r1", "kind": "external", "text": "ext item, as sent", "status": "resolved"},
        {"id": "Z3", "kind": "untested", "text": "  Untested   ITEM ", "status": "accepted"},
        {"id": "U2", "kind": "normative", "text": "norm item", "status": "open"},
    ]
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_card_respond(items))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    assert [i["id"] for i in orch.run_state.state["unverified"]] == ["U2"]
    assert _sent_ids(log) == {"U2"}
    assert "dropped 1 item(s) resolved on Urutau" in out.report_text
    assert "dropped 1 item(s) accepted on Urutau" in out.report_text


@pytest.mark.parametrize(
    "board_error",
    [err({"error": "server-error", "message": "boom"}), ToolReply(True, None, "bad schema")],
)
def test_unreachable_urutau_carries_as_is_and_says_so(repo, board_error):
    prev = _reviewed_first_run(repo)
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_card_respond([], board_error=board_error))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    state = orch.run_state.state
    assert [i["id"] for i in state["unverified"]] == ["U1", "U2", "U3"]
    assert _sent_ids(log) == {"U1", "U2", "U3"}
    assert state["urutau"]["carry_check"]["status"] == "unreachable"
    assert any(e["event"] == "warning" and e.get("source") == "urutau" for e in _events(orch))
    assert "Urutau unreachable when carrying; carried unverified items as-is" in out.report_text
    assert f"carried from {prev.id}: 3" in out.report_text
    assert TOKEN not in run_files(orch)


def test_card_without_item_list_carries_as_is(repo):
    _reviewed_first_run(repo)
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_card_respond(None))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    assert [n for n, _ in log] == ["get_board", "record_run", "record_run"]
    assert _sent_ids(log) == {"U1", "U2", "U3"}
    assert orch.run_state.state["urutau"]["carry_check"] == {"status": "unchecked"}
    assert "Urutau card has no unverified item list; carried as-is" in out.report_text


@pytest.mark.usefixtures("ordered_run_ids")
def test_carry_chains_through_run_that_failed_before_review(repo):
    first = _reviewed_first_run(repo)
    resolved = [{"id": "U1", "kind": "external", "text": "ext item", "status": "resolved"}]
    log2: list = []
    orch2, _ = make(repo, {}, log=log2, respond=_card_respond(resolved), size="S")
    assert asyncio.run(orch2.run("fix round")).status != "done"
    second = orch2.run_state
    assert [i["id"] for i in second.state["unverified"]] == ["U2", "U3"]

    # The card no longer lists U1 at all: only run 2's state keeps it away.
    log3: list = []
    orch3, _ = make(repo, S_SCRIPT, log=log3, respond=_card_respond(None))
    out = asyncio.run(orch3.run("fix round again"))
    assert out.status == "done"
    state = orch3.run_state.state
    assert [(i["id"], i["carried_from"]) for i in state["unverified"]] == [
        ("U2", second.id),
        ("U3", second.id),
    ]
    assert "U1" not in _sent_ids(log3)
    assert first.id not in out.report_text


@pytest.mark.usefixtures("ordered_run_ids")
def test_run_that_dropped_every_item_ends_the_chain(repo):
    _reviewed_first_run(repo)
    resolved = [
        {"id": u, "kind": k, "text": t, "status": "resolved"}
        for u, k, t in [
            ("U1", "external", "ext item"),
            ("U2", "normative", "norm item"),
            ("U3", "untested", "untested item"),
        ]
    ]
    log2: list = []
    orch2, _ = make(repo, {}, log=log2, respond=_card_respond(resolved), size="S")
    assert asyncio.run(orch2.run("fix round")).status != "done"
    assert orch2.run_state.state["unverified"] == []
    assert "dropped 3 item(s) resolved on Urutau" in (orch2.run_state.dir / "report.md").read_text()

    log3: list = []
    orch3, _ = make(repo, S_SCRIPT, log=log3, respond=_card_respond(None))
    out = asyncio.run(orch3.run("fix round again"))
    assert out.status == "done"
    assert orch3.run_state.state["unverified"] == []
    assert "carry_check" not in orch3.run_state.state["urutau"]
    assert _sent_ids(log3) == set()
    assert not any(e["event"] == "unverified_carried" for e in _events(orch3))
    # Nothing to carry: no extra board read before the usual start call.
    assert [n for n, _ in log3] == ["get_board", "record_run", "record_run"]


@pytest.mark.usefixtures("ordered_run_ids")
def test_new_item_after_every_carried_item_resolved_gets_fresh_id(repo):
    _reviewed_first_run(repo)
    resolved = [
        {"id": u, "kind": k, "text": t, "status": "resolved"}
        for u, k, t in [
            ("U1", "external", "ext item"),
            ("U2", "normative", "norm item"),
            ("U3", "untested", "untested item"),
        ]
    ]
    second = {**REVIEW_OK, "unverified": [{"id": "N", "kind": "untested", "text": "fresh"}]}
    log2: list = []
    orch2, _ = make(
        repo,
        {"implement": [impl()], "test": [TEST_OK], "review": [second]},
        log=log2,
        respond=_card_respond(resolved),
        size="S",
        review_small=True,
    )
    assert asyncio.run(orch2.run("fix round")).status == "done"
    new_ids = [i["id"] for i in orch2.run_state.state["unverified"]]
    assert new_ids == ["U5"]
    assert _sent_ids(log2) == {"U5"}

    # The card keeps the resolved U1..U3; the new open item must not match them.
    card = [*resolved, {"id": "U5", "kind": "untested", "text": "fresh", "status": "open"}]
    log3: list = []
    orch3, _ = make(repo, S_SCRIPT, log=log3, respond=_card_respond(card))
    assert asyncio.run(orch3.run("fix round again")).status == "done"
    assert [i["id"] for i in orch3.run_state.state["unverified"]] == ["U5"]
    assert _sent_ids(log3) == {"U5"}


def test_item_accepted_on_card_is_not_carried_or_resent(repo):
    prev = _reviewed_first_run(repo)
    items = [
        {"id": "U1", "kind": "external", "text": "ext item", "status": "open"},
        {"id": "U2", "kind": "normative", "text": "norm item", "status": "accepted"},
    ]
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_card_respond(items))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    state = orch.run_state.state
    assert [i["id"] for i in state["unverified"]] == ["U1", "U3"]
    assert "U2" not in _sent_ids(log)
    assert state["urutau"]["carry_check"] == {
        "status": "ok",
        "dropped": 1,
        "accepted": 1,
        "from": prev.id,
    }
    assert "dropped 1 item(s) accepted on Urutau" in out.report_text
    assert "resolved on Urutau" not in out.report_text


LONG_TEXT = "the vendor API documents a 429 reply under sustained load; not reproduced here"
LONG_REVIEW = {
    **REVIEW_OK,
    "unverified": [
        {"id": "U1", "kind": "external", "text": LONG_TEXT},
        {"id": "U2", "kind": "external", "text": "the vendor API documents paging; unchecked"},
        {"id": "U3", "kind": "untested", "text": LONG_TEXT},
        {"id": "U4", "kind": "external", "text": "probed away"},
    ],
}


def _cut(text: str, n: int) -> str:
    """``text`` as Urutau's cleanText shows it when capped at ``n`` code points."""
    return text[: n - 1] + "\u2026"


def test_truncated_card_text_without_id_match_is_dropped_by_prefix(repo):
    _reviewed_first_run(repo, LONG_REVIEW)
    items = [
        # No id match; the prefix is long and only U1 among external items has it.
        {"id": "X1", "kind": "external", "text": _cut(LONG_TEXT, 40), "status": "resolved"},
        # U3 matches an open card item by id: its text never falls back.
        {"id": "U3", "kind": "untested", "text": LONG_TEXT, "status": "open"},
        {"id": "X3", "kind": "untested", "text": _cut(LONG_TEXT, 40), "status": "accepted"},
    ]
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_card_respond(items))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    assert [i["id"] for i in orch.run_state.state["unverified"]] == ["U2", "U3"]
    assert _sent_ids(log) == {"U2", "U3"}
    assert "dropped 1 item(s) resolved on Urutau" in out.report_text


@pytest.mark.parametrize(
    "card_text",
    [
        _cut(LONG_TEXT, 12),  # too short to trust as a prefix
        "the vendor API documents\u2026",  # shared by U1 and U2
    ],
)
def test_short_or_ambiguous_card_prefix_is_still_carried(repo, card_text):
    _reviewed_first_run(repo, LONG_REVIEW)
    items = [{"id": "X1", "kind": "external", "text": card_text, "status": "resolved"}]
    log: list = []
    orch, _ = make(repo, S_SCRIPT, log=log, respond=_card_respond(items))
    out = asyncio.run(orch.run("fix round"))
    assert out.status == "done"
    assert [i["id"] for i in orch.run_state.state["unverified"]] == ["U1", "U2", "U3"]
    assert _sent_ids(log) == {"U1", "U2", "U3"}
    assert "on Urutau" not in out.report_text


@pytest.mark.usefixtures("ordered_run_ids")
def test_item_closed_on_card_after_run_that_failed_before_review(repo):
    first = _reviewed_first_run(repo)
    open_items = [
        {"id": u, "kind": k, "text": t, "status": "open"}
        for u, k, t in [
            ("U1", "external", "ext item"),
            ("U2", "normative", "norm item"),
            ("U3", "untested", "untested item"),
        ]
    ]
    log2: list = []
    orch2, _ = make(repo, {}, log=log2, respond=_card_respond(open_items), size="S")
    assert asyncio.run(orch2.run("fix round")).status != "done"
    second = orch2.run_state
    assert [i["id"] for i in second.state["unverified"]] == ["U1", "U2", "U3"]

    # Between run 2 and run 3 a person accepts U2 and a probe resolves U3.
    closed = [
        open_items[0],
        {**open_items[1], "status": "accepted"},
        {**open_items[2], "status": "resolved"},
    ]
    log3: list = []
    orch3, _ = make(repo, S_SCRIPT, log=log3, respond=_card_respond(closed))
    out = asyncio.run(orch3.run("fix round again"))
    assert out.status == "done"
    state = orch3.run_state.state
    assert [(i["id"], i["carried_from"]) for i in state["unverified"]] == [("U1", second.id)]
    assert _sent_ids(log3) == {"U1"}
    assert "dropped 1 item(s) resolved on Urutau" in out.report_text
    assert "dropped 1 item(s) accepted on Urutau" in out.report_text
    assert first.id not in out.report_text
