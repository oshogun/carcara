"""Fail if packaged data (``src/carcara/data/**``) is missing from a built wheel or sdist.

Usage: python scripts/check_dist.py dist/*.whl dist/*.tar.gz
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
DATA_REL = Path("carcara") / "data"


def expected_data_files(src_root: Path = SRC_ROOT) -> list[str]:
    """Package-relative paths (``carcara/data/...``) of every data file in the source tree."""
    out = []
    for path in (src_root / DATA_REL).rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            out.append(path.relative_to(src_root).as_posix())
    return sorted(out)


def archive_names(dist: Path) -> set[str]:
    """Package-relative member names: wheel members as-is, sdist members minus ``<name>/src/``."""
    if dist.suffix == ".whl":
        with zipfile.ZipFile(dist) as zf:
            return set(zf.namelist())
    if dist.name.endswith(".tar.gz"):
        with tarfile.open(dist) as tf:
            names = set()
            for name in tf.getnames():
                parts = name.split("/", 2)
                if len(parts) == 3 and parts[1] == "src":
                    names.add(parts[2])
            return names
    raise ValueError(f"unsupported distribution: {dist}")


def missing_from(dist: Path, src_root: Path = SRC_ROOT) -> list[str]:
    names = archive_names(dist)
    return [rel for rel in expected_data_files(src_root) if rel not in names]


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_dist.py DIST...", file=sys.stderr)
        return 2
    if not expected_data_files():
        print(f"no data files found under {SRC_ROOT / DATA_REL}", file=sys.stderr)
        return 1
    status = 0
    for arg in argv:
        missing = missing_from(Path(arg))
        if missing:
            status = 1
            print(f"{arg}: missing {len(missing)} data file(s):", file=sys.stderr)
            for rel in missing:
                print(f"  {rel}", file=sys.stderr)
        else:
            print(f"{arg}: ok")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
