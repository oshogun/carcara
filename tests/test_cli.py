import subprocess
import sys
from pathlib import Path

import pytest

from carcara import __version__
from carcara.cli import main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-V"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"carcara {__version__}"


def test_version_is_0_2_0():
    assert __version__ == "0.3.0"


def test_distribution_name_and_script():
    tomllib = pytest.importorskip("tomllib")
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text())["project"]
    assert project["name"] == "carcara-sdlc"
    assert project["version"] == __version__
    assert project["scripts"] == {"carcara": "carcara.cli:main"}


def test_help_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-h"])
    assert exc.value.code == 0
    assert "usage: carcara" in capsys.readouterr().out


def test_module_entry_point():
    out = subprocess.run(
        [sys.executable, "-m", "carcara", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == f"carcara {__version__}"
