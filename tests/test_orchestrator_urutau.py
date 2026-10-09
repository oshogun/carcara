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
    REVIEW_OK,
    TEST_OK,
    TRIAGE_S,
    impl,
)

from carcara.backend import FakeBackend
from carcara.orchestrator import AutoGate, Orchestrator, RunOptions
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
