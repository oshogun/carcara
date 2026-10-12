import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from carcara.runstore import (
    PROTECTED_STATUSES,
    PruneDecision,
    RunStore,
    RunStoreError,
    open_unverified_items,
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


def _select(entries, keep=None, older_than=None, active=None, include_carry_sources=False):
    return select_prunable(
        entries,
        keep=keep,
        older_than=older_than,
        now=NOW,
        active_run_id=active,
        include_carry_sources=include_carry_sources,
    )


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


# -- carry sources (#47) --------------------------------------------------------

OPEN = [{"id": "U1", "kind": "claim", "text": "unchecked"}]
CLOSED = [{"id": "U1", "kind": "claim", "text": "checked", "resolved": True}]


def _issue_state(issue=7, repo="org/repo", unverified=OPEN, **extra):
    return _state(issue=issue, urutau={"repo": repo}, unverified=unverified, **extra)


def test_open_unverified_items_filters_closed_and_incomplete():
    state = {"unverified": [*OPEN, *CLOSED, {"id": "U2"}, "junk", {"id": "U3", "text": "t"}]}
    assert [i["id"] for i in open_unverified_items(state)] == ["U1", "U3"]
    assert open_unverified_items(None) == []
    assert open_unverified_items({}) == []


def test_carry_source_kept_and_no_issue_run_deleted():
    entries = [("a-src", _issue_state()), ("b-none", _state())]
    decisions = _by_id(_select(entries, older_than=timedelta(days=30)))
    assert decisions["a-src"].action == "keep"
    assert decisions["a-src"].reason == "carry source for #7"
    assert decisions["b-none"].action == "delete"


def test_only_newest_carry_source_per_issue_kept():
    entries = [("a-old", _issue_state()), ("b-new", _issue_state())]
    decisions = _by_id(_select(entries, older_than=timedelta(days=30)))
    assert decisions["a-old"].action == "delete"
    assert decisions["b-new"].reason == "carry source for #7"


@pytest.mark.parametrize("unverified", [CLOSED, []])
def test_all_closed_run_not_a_carry_source(unverified):
    entries = [("a", _issue_state(unverified=unverified))]
    assert _deleted(_select(entries, older_than=timedelta(days=30))) == ["a"]


def test_include_carry_sources_prunes_them():
    entries = [("a-src", _issue_state())]
    decisions = _select(entries, older_than=timedelta(days=30), include_carry_sources=True)
    assert _deleted(decisions) == ["a-src"]


def test_carry_sources_grouped_per_repo_case_insensitive():
    entries = [
        ("a-other", _issue_state(repo="org/other")),
        ("b-repo", _issue_state(repo="Org/Repo")),
        ("c-repo", _issue_state(repo="org/repo")),
    ]
    decisions = _by_id(_select(entries, older_than=timedelta(days=30)))
    assert decisions["a-other"].reason == "carry source for #7"
    assert decisions["b-repo"].action == "delete"
    assert decisions["c-repo"].reason == "carry source for #7"


def test_run_without_inventory_is_not_a_carry_source():
    no_inventory = _state(issue=7, urutau={"repo": "org/repo"})
    entries = [("a-src", _issue_state()), ("b-legacy", no_inventory)]
    decisions = _by_id(_select(entries, older_than=timedelta(days=30)))
    assert decisions["a-src"].reason == "carry source for #7"
    assert decisions["b-legacy"].action == "delete"


def test_active_run_skipped_when_finding_carry_source():
    entries = [("a-src", _issue_state()), ("b-active", _issue_state())]
    decisions = _by_id(_select(entries, older_than=timedelta(days=30), active="b-active"))
    assert decisions["a-src"].reason == "carry source for #7"
    assert decisions["b-active"].reason == "active lock"


def test_newest_all_closed_run_ends_the_chain():
    entries = [("a-open", _issue_state()), ("b-closed", _issue_state(unverified=CLOSED))]
    assert _deleted(_select(entries, older_than=timedelta(days=30))) == ["a-open", "b-closed"]


def test_within_keep_reason_wins_over_carry_source():
    entries = [("a-src", _issue_state())]
    assert _select(entries, keep=1)[0].reason == "within --keep"


# -- RunStore.prune / delete_run -----------------------------------------------


def _make_run(store, run_id, status="done", days_ago=40, **extra):
    run = store.create("task", "balanced", "abc", run_id=run_id)
    run.set_status(status)
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    state["updated_at"] = state["created_at"] = _ts(days_ago)
    state.update(extra)
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


def test_prune_keeps_carry_source_on_disk(tmp_path):
    store = RunStore(tmp_path)
    src, plain = "20260101-000001-abcd", "20260101-000002-abcd"
    _make_run(store, src, issue=7, urutau={"repo": "org/repo"}, unverified=OPEN)
    _make_run(store, plain)

    decisions = store.prune(keep=None, older_than=timedelta(days=30), dry_run=False, now=NOW)
    assert _by_id(decisions)[src].reason == "carry source for #7"
    assert store.list_runs() == [src]

    store.prune(
        keep=None,
        older_than=timedelta(days=30),
        dry_run=False,
        now=NOW,
        include_carry_sources=True,
    )
    assert store.list_runs() == []
