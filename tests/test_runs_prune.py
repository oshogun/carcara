import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from carcara.runstore import (
    PROTECTED_STATUSES,
    PruneDecision,
    RunStore,
    RunStoreError,
    select_prunable,
)

# Above Linux's pid_max (2**22), so never a live process.
DEAD_PID = 2**31 - 1
NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def _ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _state(status="done", days_ago=40, **extra):
    return {"status": status, "updated_at": _ts(days_ago), **extra}


def _ids(n):
    return [f"20260101-0000{i:02d}-abcd" for i in range(n)]


def _by_id(decisions):
    return {d.run_id: d for d in decisions}


def _deleted(decisions):
    return [d.run_id for d in decisions if d.action == "delete"]


def _select(entries, keep=None, older_than=None, active=None):
    return select_prunable(entries, keep=keep, older_than=older_than, now=NOW, active_run_id=active)


# -- select_prunable ----------------------------------------------------------


def test_keep_only_deletes_oldest():
    ids = _ids(5)
    decisions = _select([(i, _state()) for i in ids], keep=2)
    assert _deleted(decisions) == ids[:3]
    assert {_by_id(decisions)[i].reason for i in ids[3:]} == {"within --keep"}


def test_older_than_only():
    entries = [("a", _state(days_ago=40)), ("b", _state(days_ago=10))]
    decisions = _select(entries, older_than=timedelta(days=30))
    assert _deleted(decisions) == ["a"]
    assert _by_id(decisions)["b"].reason == "too recent"


def test_both_flags_require_failing_both_windows():
    entries = [
        ("1-old-outside", _state(days_ago=40)),
        ("2-recent-outside", _state(days_ago=5)),
        ("3-old-inside", _state(days_ago=40)),
    ]
    decisions = _by_id(_select(entries, keep=1, older_than=timedelta(days=30)))
    assert decisions["1-old-outside"].action == "delete"
    assert decisions["2-recent-outside"].reason == "too recent"
    assert decisions["3-old-inside"].reason == "within --keep"


@pytest.mark.parametrize("status", PROTECTED_STATUSES)
def test_protected_statuses_never_deleted(status):
    assert set(PROTECTED_STATUSES) == {
        "running",
        "awaiting_approval",
        "needs_human",
        "budget_exceeded",
    }
    decisions = _select([("r", _state(status=status, days_ago=400))], keep=0)
    assert decisions[0].action == "keep"
    assert decisions[0].reason == f"status {status}"


def test_hooks_paused_statuses_still_importable():
    from carcara import hooks, runstore

    assert hooks.PAUSED_STATUSES == runstore.PAUSED_STATUSES


def test_active_lock_run_kept():
    decisions = _select([("r", _state())], keep=0, older_than=timedelta(days=1), active="r")
    assert (decisions[0].action, decisions[0].reason) == ("keep", "active lock")


def test_keep_counts_protected_runs():
    ids = _ids(3)
    entries = [(ids[0], _state()), (ids[1], _state()), (ids[2], _state(status="needs_human"))]
    decisions = _select(entries, keep=2)
    assert _deleted(decisions) == [ids[0]]


def test_unreadable_state_kept():
    decisions = _select([("r", None)], keep=0)
    assert decisions[0] == PruneDecision("r", None, None, "keep", "unreadable")


def test_age_falls_back_to_created_at_then_run_id():
    older = timedelta(days=30)
    created = {"status": "done", "created_at": _ts(40)}
    assert _deleted(_select([("x", created)], older_than=older)) == ["x"]
    assert _deleted(_select([("20200101-000000-abcd", {"status": "done"})], older_than=older)) == [
        "20200101-000000-abcd"
    ]
    garbage = {"status": "done", "updated_at": "nope", "created_at": 7}
    decisions = _select([("not-a-ts", garbage)], older_than=older)
    assert (decisions[0].action, decisions[0].reason) == ("keep", "unknown age")


def test_needs_keep_or_older_than():
    with pytest.raises(ValueError):
        _select([("r", _state())])


def test_keep_zero_deletes_every_unprotected_run():
    entries = [("a", _state()), ("b", _state(status="running")), ("c", _state(days_ago=0))]
    assert _deleted(_select(entries, keep=0)) == ["a", "c"]


# -- RunStore.prune / delete_run -----------------------------------------------


def _make_run(store, run_id, status="done", days_ago=40):
    run = store.create("task", "balanced", "abc", run_id=run_id)
    run.set_status(status)
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    state["updated_at"] = state["created_at"] = _ts(days_ago)
    run.state_path.write_text(json.dumps(state), encoding="utf-8")
    return run


def test_prune_dry_run_then_real(tmp_path):
    store = RunStore(tmp_path)
    ids = _ids(4)
    for i in ids:
        _make_run(store, i)
    _make_run(store, "20260101-000099-paus", status="awaiting_approval")

    decisions = store.prune(keep=None, older_than=timedelta(days=30), dry_run=True, now=NOW)
    assert _deleted(decisions) == ids
    assert len(store.list_runs()) == 5

    # --keep counts the paused run too, so keep=2 leaves it and one done run.
    decisions = store.prune(keep=2, older_than=None, dry_run=False, now=NOW)
    assert _deleted(decisions) == ids[:3]
    assert store.list_runs() == [ids[3], "20260101-000099-paus"]


def test_prune_never_deletes_live_lock_holder(tmp_path):
    store = RunStore(tmp_path)
    _make_run(store, "20260101-000000-lock")
    store.acquire_lock("20260101-000000-lock")  # live pid: this process
    decisions = store.prune(keep=0, older_than=timedelta(days=1), dry_run=False, now=NOW)
    assert (decisions[0].action, decisions[0].reason) == ("keep", "active lock")
    assert store.list_runs() == ["20260101-000000-lock"]


def test_prune_stale_lock_does_not_protect(tmp_path):
    store = RunStore(tmp_path)
    _make_run(store, "20260101-000000-lock")
    store.lock_path.write_text(
        json.dumps({"pid": DEAD_PID, "run_id": "20260101-000000-lock"}), encoding="utf-8"
    )
    decisions = store.prune(keep=0, older_than=None, dry_run=False, now=NOW)
    assert _deleted(decisions) == ["20260101-000000-lock"]
    assert store.list_runs() == []


def test_prune_rechecks_lock_before_deleting(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    _make_run(store, "r1")
    calls = []

    def fake_active():
        calls.append(1)
        return None if len(calls) == 1 else {"run_id": "r1", "pid": os.getpid()}

    monkeypatch.setattr(store, "active", fake_active)
    decisions = store.prune(keep=0, older_than=None, dry_run=False, now=NOW)
    assert (decisions[0].action, decisions[0].reason) == ("keep", "active lock")
    assert store.list_runs() == ["r1"]


def test_prune_records_deletion_errors(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    _make_run(store, "r1")
    _make_run(store, "r2")

    def boom(run_id):
        if run_id == "r1":
            raise OSError("denied")
        RunStore.delete_run(store, run_id)

    monkeypatch.setattr(store, "delete_run", boom)
    decisions = _by_id(store.prune(keep=0, older_than=None, dry_run=False, now=NOW))
    assert decisions["r1"].action == "keep"
    assert decisions["r1"].reason == "error: denied"
    assert decisions["r2"].action == "delete"


def test_prune_unreadable_state_kept_on_disk(tmp_path):
    store = RunStore(tmp_path)
    run = _make_run(store, "r1")
    run.state_path.write_text("{not json", encoding="utf-8")
    decisions = store.prune(keep=0, older_than=None, dry_run=False, now=NOW)
    assert decisions[0].reason == "unreadable"
    assert run.dir.is_dir()


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b"])
def test_delete_run_rejects_invalid_ids(tmp_path, bad):
    store = RunStore(tmp_path)
    store.root.mkdir(parents=True)
    (tmp_path / "x").mkdir()
    with pytest.raises(RunStoreError):
        store.delete_run(bad)
    assert (tmp_path / "x").is_dir()


def test_delete_run_does_not_follow_symlink(tmp_path, symlink):
    store = RunStore(tmp_path / "proj")
    store.root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    symlink(store.root / "link", outside, target_is_directory=True)
    with pytest.raises(RunStoreError):
        store.delete_run("link")
    assert (outside / "keep.txt").exists()
