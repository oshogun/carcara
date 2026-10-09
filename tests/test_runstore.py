import json
import os
import sys

import pytest

from carcara import runstore
from carcara.runstore import RunBusy, RunStore, RunStoreError

# Above Linux's pid_max (2**22), so never a live process.
DEAD_PID = 2**31 - 1


def _write_lock(store, data):
    store.base.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    store.lock_path.write_text(text, encoding="utf-8")


def _events(run):
    lines = (run.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# -- create / load / save ---------------------------------------------------


def test_create_initialises_state_and_gitignore(tmp_path):
    store = RunStore(tmp_path)
    run = store.create("do it", "balanced", "abc123", size="S")

    assert run.dir == store.root / run.id
    assert (store.base / ".gitignore").read_text() == "*\n"
    state = json.loads(run.state_path.read_text())
    assert state["run_id"] == run.id
    assert state["task"] == "do it"
    assert state["profile"] == "balanced"
    assert state["size"] == "S"
    assert state["base_sha"] == "abc123"
    assert state["status"] == "running"
    assert state["stages"] == [] and state["failed_attempts"] == []
    assert state["totals"] == {
        "cost_usd": 0.0,
        "num_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert state["triage_range"] is None and state["uncertainty_kind"] is None
    assert state["issue"] is None and state["card_estimate"] is None
    assert state["plan_rejected"] is False
    assert state["urutau"] == {
        "enabled": False,
        "repo": None,
        "issue": None,
        "sent_items": {},
        "last": None,
    }
    (started,) = _events(run)
    assert started["event"] == "run_started"
    assert started["task"] == "do it" and started["base_sha"] == "abc123"
    assert "ts" in started


def test_create_keeps_existing_gitignore(tmp_path):
    store = RunStore(tmp_path)
    store.base.mkdir()
    (store.base / ".gitignore").write_text("custom\n")
    store.create("t", "balanced", "sha")
    assert (store.base / ".gitignore").read_text() == "custom\n"


def test_state_round_trip(tmp_path):
    store = RunStore(tmp_path)
    run = store.create("t", "balanced", "sha")
    run.state["plan_approved"] = True
    run.add_cost(0.25, {"input_tokens": 10, "output_tokens": 5}, 3)
    run.record_stage("explore", "explore", "explorer", "m", {"x": 1}, 0.25, None, 3, "sess")
    run.set_status("done", "all good")

    loaded = store.load(run.id)
    assert loaded.state == run.state
    assert loaded.dir == run.dir
    assert loaded.stage("explore")["output"] == {"x": 1}
    assert loaded.stage("missing") is None
    assert loaded.state["totals"]["cost_usd"] == 0.25
    assert loaded.state["totals"]["input_tokens"] == 10
    assert loaded.state["status"] == "done" and loaded.state["message"] == "all good"
    # Atomic save leaves no temp files behind.
    assert sorted(p.name for p in run.dir.iterdir()) == ["events.jsonl", "state.json"]


def test_record_stage_rejects_duplicate_key(tmp_path):
    run = RunStore(tmp_path).create("t", "balanced", "sha")
    run.record_stage("k", "s", None, "m", None, 0.0, None, 0, None)
    with pytest.raises(RunStoreError, match="already recorded"):
        run.record_stage("k", "s", None, "m", None, 0.0, None, 0, None)


def test_record_failed_attempt_counts_uncounted(tmp_path):
    run = RunStore(tmp_path).create("t", "balanced", "sha")
    run.record_failed_attempt("k", "s", "r", "m", "x" * 600, 0.5, None, 1, counted=False)
    run.record_failed_attempt("k", "s", "r", "m", "boom", 0.5, None, 1, counted=True)
    first, second = run.state["failed_attempts"]
    assert len(first["error"]) == 500 and first["error"].endswith("...")
    assert first["cost_usd"] is None
    assert second["cost_usd"] == 0.5
    assert run.state["totals"]["uncounted_stages"] == 1


def test_set_status_rejects_unknown(tmp_path):
    run = RunStore(tmp_path).create("t", "balanced", "sha")
    with pytest.raises(ValueError):
        run.set_status("bogus")


def test_event_appends_in_order(tmp_path):
    run = RunStore(tmp_path).create("t", "balanced", "sha")
    run.event("one", n=1)
    run.event("two", n=2)
    run.set_status("failed", "nope")
    events = _events(run)
    assert [e["event"] for e in events] == ["run_started", "one", "two", "status"]
    assert events[1]["n"] == 1 and events[2]["n"] == 2
    assert events[3]["status"] == "failed" and events[3]["message"] == "nope"


def test_report_round_trip(tmp_path):
    run = RunStore(tmp_path).create("t", "balanced", "sha")
    assert run.read_report() is None
    run.write_report("# hi")
    assert run.read_report() == "# hi\n"


@pytest.mark.parametrize("run_id", ["", ".", "..", "a/b"])
def test_load_rejects_invalid_ids(tmp_path, run_id):
    with pytest.raises(RunStoreError, match="invalid run id"):
        RunStore(tmp_path).load(run_id)


def test_load_missing_run(tmp_path):
    with pytest.raises(RunStoreError, match="unknown run"):
        RunStore(tmp_path).load("nope")


def test_load_corrupt_state(tmp_path):
    store = RunStore(tmp_path)
    run = store.create("t", "balanced", "sha")
    run.state_path.write_text("{not json")
    with pytest.raises(RunStoreError, match="cannot read"):
        store.load(run.id)


# -- listing ----------------------------------------------------------------


def test_list_runs_empty_without_root(tmp_path):
    assert RunStore(tmp_path).list_runs() == []


def test_list_runs_sorted_and_skips_dirs_without_state(tmp_path):
    store = RunStore(tmp_path)
    store.root.mkdir(parents=True)
    for name in ("20260102-000000-bbbb", "20260101-000000-aaaa", "20260103-000000-cccc"):
        (store.root / name).mkdir()
        (store.root / name / "state.json").write_text("{}")
    (store.root / "20260104-000000-dddd").mkdir()  # no state.json
    (store.root / "stray.txt").write_text("x")
    assert store.list_runs() == [
        "20260101-000000-aaaa",
        "20260102-000000-bbbb",
        "20260103-000000-cccc",
    ]


def test_create_allocates_distinct_ids(tmp_path):
    store = RunStore(tmp_path)
    ids = {store.create("t", "balanced", "sha").id for _ in range(5)}
    assert len(ids) == 5
    assert store.list_runs() == sorted(ids)


# -- active-run lock --------------------------------------------------------


def test_acquire_and_release_lock(tmp_path):
    store = RunStore(tmp_path)
    assert store.active() is None
    store.acquire_lock("run-1")
    holder = store.active()
    assert holder["run_id"] == "run-1" and holder["pid"] == os.getpid()
    # No temp/aside files left behind.
    assert sorted(p.name for p in store.base.iterdir()) == [".gitignore", "active.json", "runs"]

    store.release_lock("run-1")
    assert not store.lock_path.exists()
    assert store.active() is None


def test_acquire_lock_busy_when_live_holder(tmp_path):
    store = RunStore(tmp_path)
    store.acquire_lock("run-1")
    with pytest.raises(RunBusy) as exc:
        store.acquire_lock("run-2")
    assert exc.value.run_id == "run-1"
    assert store.active()["run_id"] == "run-1"


def test_release_lock_ignores_other_run_or_pid(tmp_path):
    store = RunStore(tmp_path)
    store.acquire_lock("run-1")
    store.release_lock("run-2")
    assert store.lock_path.exists()

    _write_lock(store, {"pid": DEAD_PID, "run_id": "run-1"})
    store.release_lock("run-1")
    assert store.lock_path.exists()


def test_release_lock_without_lock_is_noop(tmp_path):
    RunStore(tmp_path).release_lock("run-1")


def test_stale_lock_dead_pid_is_taken_over(tmp_path):
    store = RunStore(tmp_path)
    _write_lock(store, {"pid": DEAD_PID, "run_id": "old", "started": "x"})
    assert store.active() is None
    store.acquire_lock("new")
    assert store.active()["run_id"] == "new"
    assert not list(store.base.glob(".active.json.*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc start time")
def test_stale_lock_reused_pid_is_taken_over(tmp_path):
    store = RunStore(tmp_path)
    _write_lock(store, {"pid": os.getpid(), "run_id": "old", "start": "not-the-start"})
    assert store.active() is None
    store.acquire_lock("new")
    assert store.active()["run_id"] == "new"


def test_lock_without_start_uses_pid_only(tmp_path):
    store = RunStore(tmp_path)
    _write_lock(store, {"pid": os.getpid(), "run_id": "old"})
    assert store.active()["run_id"] == "old"
    with pytest.raises(RunBusy):
        store.acquire_lock("new")


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        json.dumps({"pid": os.getpid()}),
        json.dumps({"pid": os.getpid(), "run_id": 7}),
    ],
)
def test_corrupt_lock_is_taken_over(tmp_path, content):
    store = RunStore(tmp_path)
    _write_lock(store, content)
    assert store.active() is None
    store.acquire_lock("new")
    assert store.active()["run_id"] == "new"


@pytest.mark.parametrize("pid", [None, "123", True, 0, -1, 1.5])
def test_lock_with_invalid_pid_is_stale(tmp_path, pid):
    store = RunStore(tmp_path)
    _write_lock(store, {"pid": pid, "run_id": "old"})
    assert store.active() is None


def test_take_over_restores_replaced_lock(tmp_path, monkeypatch):
    """A lock rewritten between the stale check and the takeover is put back."""
    store = RunStore(tmp_path)
    _write_lock(store, {"pid": DEAD_PID, "run_id": "old"})
    live = json.dumps({"pid": os.getpid(), "run_id": "racer"})
    real_rename = os.rename

    def racing_rename(src, dst):
        if str(src) == str(store.lock_path):
            store.lock_path.write_text(live, encoding="utf-8")
        return real_rename(src, dst)

    monkeypatch.setattr(runstore.os, "rename", racing_rename)
    with pytest.raises(RunBusy) as exc:
        store.acquire_lock("new")
    assert exc.value.run_id == "racer"
    assert store.lock_path.read_text(encoding="utf-8") == live
    assert not list(store.base.glob(".active.json.*"))
