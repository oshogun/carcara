"""Deterministic test-stage commands: discovery and bounded subprocess runs.

The test stage runs known commands itself instead of handing them to an agent,
so nothing can background a command and then poll-wait on it forever. Each
command runs in its own session (a new process group on Windows) with stdin
closed and a hard wall-clock timeout; on timeout (or cancellation) the whole
process group (on Windows the process tree, via ``taskkill /T``) is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

TAIL_LINES = 200
TAIL_CHARS = 8000
# Raw bytes kept while reading; comfortably above TAIL_CHARS so the decoded
# tail is complete even with multi-byte characters.
_TAIL_BUFFER_BYTES = 64 * 1024
_READ_CHUNK = 64 * 1024
TERM_GRACE_S = 5.0
READER_GRACE_S = 2.0
EXCERPT_CHARS_PER_COMMAND = 4000
EXCERPT_CHARS = 12000

_WINDOWS = os.name == "nt"

_RUFF_RE = re.compile(r"^\s*\[\s*tool\.ruff\s*[\].]", re.MULTILINE)
_PYTEST_RE = re.compile(r"^\s*\[\s*tool\.pytest\.ini_options\s*\]", re.MULTILINE)


def _pyproject_sections(path: Path) -> tuple[bool, bool]:
    """Return (has [tool.ruff], has [tool.pytest.ini_options])."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False, False
    if sys.version_info >= (3, 11):
        import tomllib

        try:
            tool = tomllib.loads(text).get("tool", {})
        except tomllib.TOMLDecodeError:
            tool = None
        if isinstance(tool, dict):
            pytest_cfg = tool.get("pytest")
            return (
                "ruff" in tool,
                isinstance(pytest_cfg, dict) and "ini_options" in pytest_cfg,
            )
    return bool(_RUFF_RE.search(text)), bool(_PYTEST_RE.search(text))


def discover_test_commands(cwd: str | os.PathLike[str]) -> list[str]:
    """Guess the project's check/test commands from its files, without a model.

    ``[tool.ruff]`` in pyproject.toml gives ``ruff check .`` and
    ``ruff format --check .``; ``[tool.pytest.ini_options]``, a ``pytest.ini``
    or a ``tests/`` directory gives ``pytest``. A tool is run from
    ``.venv/bin/`` (or ``.venv\\Scripts\\`` on a Windows venv) when the
    project has it there. Returns [] when nothing is recognised.
    """
    root = Path(cwd)
    has_ruff = has_pytest = False
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        has_ruff, has_pytest = _pyproject_sections(pyproject)
    if (root / "pytest.ini").is_file() or (root / "tests").is_dir():
        has_pytest = True

    def tool(name: str) -> str:
        if (root / ".venv" / "bin" / name).is_file():
            return f".venv/bin/{name}"
        if (root / ".venv" / "Scripts" / f"{name}.exe").is_file():
            return f".venv\\Scripts\\{name}"
        return name

    cmds: list[str] = []
    if has_ruff:
        cmds += [f"{tool('ruff')} check .", f"{tool('ruff')} format --check ."]
    if has_pytest:
        cmds.append(tool("pytest"))
    return cmds


@dataclass
class CommandResult:
    cmd: str
    exit_code: int | None  # None when the command timed out
    timed_out: bool
    duration_s: float
    output_tail: str
    timeout_s: float | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class _Tail:
    """Bounded buffer of the most recent output bytes."""

    def __init__(self, limit: int = _TAIL_BUFFER_BYTES) -> None:
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._limit = limit

    def add(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size - len(self._chunks[0]) >= self._limit:
            self._size -= len(self._chunks.popleft())

    def text(self) -> str:
        data = b"".join(self._chunks)[-self._limit :]
        lines = data.decode("utf-8", errors="replace").splitlines()
        text = "\n".join(lines[-TAIL_LINES:])
        return text[-TAIL_CHARS:]


async def _read(stream: asyncio.StreamReader, tail: _Tail) -> None:
    while chunk := await stream.read(_READ_CHUNK):
        tail.add(chunk)


def _killpg(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _kill_tree(pid: int) -> None:
    """Forcefully kill ``pid`` and all its descendants (Windows only)."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            check=False,
            timeout=10,
        )


async def _kill_group(proc: asyncio.subprocess.Process, pgid: int) -> None:
    """SIGTERM the group, give it TERM_GRACE_S to exit, then SIGKILL it.

    On Windows there are no process-group signals: the process tree is killed
    with ``taskkill /T /F`` and the shell is given TERM_GRACE_S to be reaped.
    """
    if _WINDOWS:
        _kill_tree(proc.pid)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), TERM_GRACE_S)
        return
    _killpg(pgid, signal.SIGTERM)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), TERM_GRACE_S)
    _killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), TERM_GRACE_S)
    await _wait_group_gone(pgid, READER_GRACE_S)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_group_gone(pgid: int, grace_s: float) -> None:
    """Wait (bounded) until every group member has been reaped.

    Orphaned children are reparented and reaped asynchronously by init or a
    subreaper, so they can linger briefly as zombies after SIGKILL.
    """
    deadline = time.monotonic() + grace_s
    while _group_alive(pgid) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def run_command(
    cmd: str,
    cwd: str | os.PathLike[str],
    timeout_s: float,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run ``cmd`` through the shell in ``cwd`` with a hard wall-clock timeout.

    ``env`` replaces the inherited environment when given.

    The command gets its own session (process group; on Windows a new process
    group, run through the default shell) and a closed stdin;
    stderr is merged into stdout and only a bounded tail is kept. The timeout
    covers both the shell's exit and EOF on its output, so a backgrounded
    child holding the pipe counts as still running. On timeout the whole
    group is killed and the result is marked ``timed_out``.
    """
    start = time.monotonic()
    group_kwargs: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if _WINDOWS
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=os.fspath(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **group_kwargs,
    )
    pgid = proc.pid  # session leader: its pid is the group id
    tail = _Tail()
    assert proc.stdout is not None
    reader = asyncio.ensure_future(_read(proc.stdout, tail))

    async def finish() -> None:
        await proc.wait()
        await asyncio.shield(reader)

    timed_out = False
    try:
        await asyncio.wait_for(finish(), timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        await _kill_group(proc, pgid)
        # A grandchild that left the group may still hold the pipe open.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), READER_GRACE_S)
    except asyncio.CancelledError:
        if _WINDOWS:
            _kill_tree(proc.pid)
        else:
            _killpg(pgid, signal.SIGKILL)
        # Reap the shell so no zombie keeps the group alive past cancellation.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), READER_GRACE_S)
        reader.cancel()
        raise
    finally:
        if not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
    return CommandResult(
        cmd=cmd,
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        duration_s=round(time.monotonic() - start, 3),
        output_tail=tail.text(),
        timeout_s=timeout_s,
    )


async def run_commands(
    cmds: list[str], cwd: str | os.PathLike[str], timeout_s: float
) -> list[CommandResult]:
    """Run every command in order (even after a failure); timeout is per command."""
    return [await run_command(cmd, cwd, timeout_s) for cmd in cmds]


def failure_detail(result: CommandResult, limit: int = EXCERPT_CHARS_PER_COMMAND) -> str:
    """Short description of one failed command: timeout note or output tail."""
    if result.timed_out:
        limit_s = result.timeout_s if result.timeout_s is not None else result.duration_s
        head = f"timed out after {limit_s:g}s"
    else:
        head = f"exit code {result.exit_code}"
    tail = result.output_tail.strip()
    if len(tail) > limit:
        tail = "...\n" + tail[-limit:]
    return f"{head}\n{tail}" if tail else head


def excerpt(results: list[CommandResult], limit: int = EXCERPT_CHARS) -> str:
    """Bounded text block describing the failing commands and their output."""
    failed = [r for r in results if not r.ok]
    if not failed:
        return ""
    per = max(500, limit // len(failed) - 200)
    blocks = [f"$ {r.cmd}\n{failure_detail(r, per)}" for r in failed]
    text = "\n\n".join(blocks)
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"
