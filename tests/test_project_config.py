import json

import pytest

from carcara.project_config import (
    DEFAULT_VERIFIABILITY_PATHS,
    ProjectConfig,
    ProjectConfigError,
    load_project_config,
    match_paths,
    parse_project_config,
)


def write_config(tmp_path, data):
    d = tmp_path / ".carcara"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(data if isinstance(data, str) else json.dumps(data))


def test_defaults_when_no_file(tmp_path):
    cfg = load_project_config(str(tmp_path))
    assert cfg.verifiability_paths == list(DEFAULT_VERIFIABILITY_PATHS)
    assert cfg.probes == {}
    assert "pyproject.toml" not in cfg.verifiability_paths


def test_custom_list_overrides(tmp_path):
    write_config(tmp_path, {"verifiability_paths": ["infra/**"]})
    assert load_project_config(str(tmp_path)).verifiability_paths == ["infra/**"]


def test_empty_list_disables(tmp_path):
    write_config(tmp_path, {"verifiability_paths": []})
    cfg = load_project_config(str(tmp_path))
    assert cfg.verifiability_paths == []
    assert match_paths([".github/workflows/x.yml"], cfg.verifiability_paths) == []


def test_probes_loaded(tmp_path):
    write_config(tmp_path, {"probes": {"pypi-name": "https://pypi.org/pypi/{arg}/json"}})
    assert load_project_config(str(tmp_path)).probes == {
        "pypi-name": "https://pypi.org/pypi/{arg}/json"
    }


@pytest.mark.parametrize(
    "data",
    [
        "[]",
        "{not json",
        {"unknown": 1},
        {"verifiability_paths": "x/**"},
        {"verifiability_paths": [1]},
        {"probes": []},
        {"probes": {"p": 5}},
        {"probes": {"p": "ftp://example.com/{arg}"}},
        {"probes": {"p": "file:///etc/{arg}"}},
        {"probes": {"p": "https://example.com/x"}},
        {"probes": {"p": "https://example.com/{arg}/{arg}"}},
        {"probes": {"p": "https://user:pass@example.com/{arg}"}},
        {"probes": {"p": "https://{arg}.example.com/"}},
        {"test_commands": "pytest"},
        {"test_commands": ["pytest", ""]},
        {"test_commands": [1]},
        {"test_timeout_s": 0},
        {"test_timeout_s": -5},
        {"test_timeout_s": "60"},
        {"test_timeout_s": True},
    ],
)
def test_invalid_config_raises(tmp_path, data):
    write_config(tmp_path, data)
    with pytest.raises(ProjectConfigError):
        load_project_config(str(tmp_path))


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/x.yml",
        "./.github/workflows/x.yml",
        "a/b/migrations/001.sql",
        "migrations/001.sql",
        "src/auth/x.py",
        "src/policy.py",
        "policy.py",
    ],
)
def test_default_patterns_match(path):
    assert match_paths([path], DEFAULT_VERIFIABILITY_PATHS) == [path.removeprefix("./")]


@pytest.mark.parametrize("path", ["src/a.py", "docs/github.md", "authz.py", "src/authz/x.py"])
def test_default_patterns_do_not_match(path):
    assert match_paths([path], DEFAULT_VERIFIABILITY_PATHS) == []


def test_match_paths_glob_semantics():
    assert match_paths(["a/b.py", "a/c/b.py"], ["a/*.py"]) == ["a/b.py"]
    assert match_paths(["a/x1", "a/x12"], ["a/x?"]) == ["a/x1"]
    assert match_paths(["a.py", "./a.py", "b.py"], ["a.py"]) == ["a.py"]
    assert match_paths(["a.py"], []) == []


def test_policy_dir_matches_by_default():
    assert "**/policy/**" in DEFAULT_VERIFIABILITY_PATHS
    paths = ["src/policy/x.py", "policy.py", "a/policy_x.md", "src/policies.py", "src/x.py"]
    assert match_paths(paths, DEFAULT_VERIFIABILITY_PATHS) == paths[:3]


def test_absolute_paths_under_cwd_match_relative_patterns(tmp_path):
    inside = str(tmp_path / ".github" / "workflows" / "ci.yml")
    outside = str(tmp_path.parent / ".github" / "workflows" / "ci.yml")
    assert match_paths([inside], [".github/**"], str(tmp_path)) == [".github/workflows/ci.yml"]
    assert match_paths([outside], [".github/**"], str(tmp_path)) == []
    assert match_paths([inside], [".github/**"]) == []
    assert match_paths([str(tmp_path)], ["**"], str(tmp_path)) == [str(tmp_path)]


def test_absolute_path_via_symlinked_cwd(tmp_path, symlink):
    real = tmp_path / "real"
    (real / "db" / "migrations").mkdir(parents=True)
    link = tmp_path / "link"
    symlink(link, real, target_is_directory=True)
    path = str(real / "db" / "migrations" / "1.sql")
    assert match_paths([path], DEFAULT_VERIFIABILITY_PATHS, str(link)) == ["db/migrations/1.sql"]


def test_config_dict_round_trip():
    cfg = ProjectConfig(verifiability_paths=["a/**"], probes={"p": "https://h/{arg}"})
    assert parse_project_config(cfg.to_dict()) == cfg
    assert parse_project_config(ProjectConfig().to_dict()) == ProjectConfig()
    cfg = ProjectConfig(test_commands=["ruff check .", "pytest"], test_timeout_s=60.0)
    assert parse_project_config(cfg.to_dict()) == cfg
    assert parse_project_config(ProjectConfig(test_commands=[]).to_dict()).test_commands == []


def test_test_commands_defaults_and_loading(tmp_path):
    cfg = load_project_config(str(tmp_path))
    assert cfg.test_commands is None
    assert cfg.test_timeout_s is None
    # Snapshots from before these keys existed still parse.
    old = parse_project_config({"verifiability_paths": ["a/**"], "probes": {}})
    assert old.test_commands is None and old.test_timeout_s is None
    write_config(tmp_path, {"test_commands": ["pytest -q"], "test_timeout_s": 300})
    cfg = load_project_config(str(tmp_path))
    assert cfg.test_commands == ["pytest -q"]
    assert cfg.test_timeout_s == 300.0
    write_config(tmp_path, {"test_commands": []})
    assert load_project_config(str(tmp_path)).test_commands == []
