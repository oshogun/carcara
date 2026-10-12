import json

import pytest

from carcara.cli import main
from carcara.runstore import RunStore


def _make_runs(tmp_path, n):
    store = RunStore(tmp_path)
    ids = [f"20260101-0000{i:02d}-abcd" for i in range(n)]
    for run_id in ids:
        store.create("task", "balanced", "abc", run_id=run_id).set_status("done")
    return store, ids


def test_prune_keep_deletes(tmp_path, capsys):
    store, ids = _make_runs(tmp_path, 3)
    assert main(["runs", "prune", "--keep", "1", "--cwd", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"delete       {ids[0]}" in out
    assert "2 run(s) deleted, 1 kept" in out
    assert store.list_runs() == [ids[2]]


def test_prune_dry_run_leaves_dirs(tmp_path, capsys):
    store, ids = _make_runs(tmp_path, 2)
    assert main(["runs", "prune", "--keep", "0", "--dry-run", "--cwd", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "would delete" in out
    assert "2 run(s) would be deleted, 0 kept" in out
    assert store.list_runs() == ids


def test_prune_json(tmp_path, capsys):
    _, ids = _make_runs(tmp_path, 2)
    argv = ["runs", "prune", "--keep", "1", "--dry-run", "--json", "--cwd", str(tmp_path)]
    assert main(argv) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(d["run_id"], d["action"]) for d in data] == [(ids[0], "delete"), (ids[1], "keep")]


def test_prune_older_than_keeps_recent(tmp_path, capsys):
    store, ids = _make_runs(tmp_path, 1)
    assert main(["runs", "prune", "--older-than", "30d", "--cwd", str(tmp_path)]) == 0
    assert "too recent" in capsys.readouterr().out
    assert store.list_runs() == ids


def test_prune_without_flags_is_error(tmp_path, capsys):
    assert main(["runs", "prune", "--cwd", str(tmp_path)]) == 2
    assert "--older-than and/or --keep" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags", [["--older-than", "30x"], ["--older-than", "0d"], ["--keep", "-1"], ["--keep", "x"]]
)
def test_prune_invalid_values_rejected(tmp_path, flags):
    with pytest.raises(SystemExit) as exc:
        main(["runs", "prune", *flags, "--cwd", str(tmp_path)])
    assert exc.value.code == 2


def test_prune_missing_cwd(tmp_path, capsys):
    assert main(["runs", "prune", "--keep", "1", "--cwd", str(tmp_path / "nope")]) == 1
    assert "directory not found" in capsys.readouterr().err


def test_runs_without_action(capsys):
    assert main(["runs"]) == 2
    assert "prune" in capsys.readouterr().err


def test_prune_empty_project(tmp_path, capsys):
    assert main(["runs", "prune", "--keep", "1", "--cwd", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "0 runs"
    assert not (tmp_path / ".carcara").exists()
