import json
from datetime import datetime, timedelta

from carcara.cli import main
from carcara.runstore import RunStore


def _ts(days_ago, seconds=0):
    when = datetime.now().astimezone() - timedelta(days=days_ago) + timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S%z")


def _make_run(store, run_id, status="done", days_ago=1, tokens=100, active=60, **extra):
    run = store.create("task", "balanced", "abc", run_id=run_id)
    run.set_status(status)
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    state["updated_at"] = state["created_at"] = _ts(days_ago)
    state["stages"] = [
        {
            "key": "implement",
            "stage": "implement",
            "role": "implementer",
            "model": "opus",
            "output": {},
            "cost_usd": 0.25,
            "num_turns": 1,
            "session_id": None,
            "usage": {"input_tokens": tokens - 10, "output_tokens": 10},
        }
    ]
    state["totals"]["cost_usd"] = 0.25
    state.update(extra)
    run.state_path.write_text(json.dumps(state), encoding="utf-8")
    with (run.dir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for seconds, kind in ((0, "stage_started"), (active, "stage_completed")):
            record = {"ts": _ts(days_ago, seconds), "event": kind, "key": "implement"}
            fh.write(json.dumps(record) + "\n")
    return run


def _usage(capsys, repo, *args):
    capsys.readouterr()
    code = main(["usage", *args, "--cwd", str(repo)])
    return code, capsys.readouterr().out


def test_usage_json_groups(tmp_path, capsys):
    store = RunStore(tmp_path)
    _make_run(store, "20260101-000000-aaaa", size="S", tokens=100)
    _make_run(store, "20260101-000001-bbbb", size="S", tokens=300)
    _make_run(store, "20260101-000002-cccc", size="M", ultra=True, tokens=1000)
    code, out = _usage(capsys, tmp_path, "--json")
    assert code == 0
    data = json.loads(out)
    assert data["runs"] == 3 and data["totals"]["tokens"] == 1400
    assert data["totals"]["active_seconds"] == 180
    assert data["cost_note"] == "estimates at API prices; not subscription billing"
    groups = [(g["size"], g["mode"], g["runs"]) for g in data["by_size_mode"]]
    assert groups == [("S", "plain", 2), ("M", "ultra", 1)]
    assert data["by_size_mode"][0]["tokens"]["median"] == 200
    assert data["by_role_model"][0]["est_cost_usd"] == 0.75


def test_usage_text_cost_flag_and_since(tmp_path, capsys):
    store = RunStore(tmp_path)
    _make_run(store, "20260101-000000-aaaa", size="S", days_ago=30, tokens=5000)
    _make_run(store, "20260101-000001-bbbb", size="S", days_ago=1, tokens=100)
    code, out = _usage(capsys, tmp_path)
    assert code == 0 and "$" not in out and "runs: 2" in out
    code, out = _usage(capsys, tmp_path, "--cost")
    assert "est. cost (API-price estimate, not subscription billing): $0.50" in out
    code, out = _usage(capsys, tmp_path, "--since", "7d", "--json")
    data = json.loads(out)
    assert data["runs"] == 1 and data["totals"]["tokens"] == 100
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    code, out = _usage(capsys, tmp_path, "--until", since, "--json")
    assert json.loads(out)["totals"]["tokens"] == 5000


def test_usage_no_runs(tmp_path, capsys):
    code, out = _usage(capsys, tmp_path)
    assert code == 0 and out == "no runs\n"
    code, out = _usage(capsys, tmp_path, "--json")
    assert code == 0 and json.loads(out)["runs"] == 0


def test_usage_bad_date(tmp_path, capsys):
    try:
        main(["usage", "--since", "yesterday", "--cwd", str(tmp_path)])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse error")
    assert "invalid date" in capsys.readouterr().err
