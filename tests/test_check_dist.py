import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_dist.py"
_spec = importlib.util.spec_from_file_location("check_dist", _SCRIPT)
check_dist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_dist)


def _make_wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, "x")
    return path


def _make_sdist(path: Path, names: list[str]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name in names:
            info = tarfile.TarInfo(f"carcara_sdlc-0.0.0/src/{name}")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    return path


def test_expected_data_files_cover_profiles_and_templates():
    files = check_dist.expected_data_files()
    assert "carcara/data/profiles/balanced.env" in files
    assert "carcara/data/templates/CLAUDE.md" in files
    assert "carcara/data/templates/claude/settings.json" in files
    assert not any("__pycache__" in f for f in files)


@pytest.mark.parametrize("make, suffix", [(_make_wheel, ".whl"), (_make_sdist, ".tar.gz")])
def test_complete_dist_passes(tmp_path, make, suffix, capsys):
    dist = make(tmp_path / f"carcara_sdlc-0.0.0{suffix}", check_dist.expected_data_files())
    assert check_dist.missing_from(dist) == []
    assert check_dist.main([str(dist)]) == 0
    assert "ok" in capsys.readouterr().out


@pytest.mark.parametrize("make, suffix", [(_make_wheel, ".whl"), (_make_sdist, ".tar.gz")])
def test_missing_data_fails(tmp_path, make, suffix, capsys):
    names = [n for n in check_dist.expected_data_files() if "/templates/" not in n]
    dist = make(tmp_path / f"carcara_sdlc-0.0.0{suffix}", names)
    missing = check_dist.missing_from(dist)
    assert "carcara/data/templates/CLAUDE.md" in missing
    assert all("/templates/" in m for m in missing)
    assert check_dist.main([str(dist)]) == 1
    assert "carcara/data/templates/CLAUDE.md" in capsys.readouterr().err


def test_no_args_is_usage_error():
    assert check_dist.main([]) == 2
