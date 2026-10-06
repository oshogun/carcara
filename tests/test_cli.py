import subprocess
import sys

import pytest

from carcara import __version__
from carcara.cli import main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-V"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"carcara {__version__}"


def test_version_is_0_2_0():
    assert __version__ == "0.2.0"


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
