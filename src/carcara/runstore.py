"""Persistent state for ``carcara run`` under ``<cwd>/.carcara/runs/<run id>/``.

Each run directory holds ``state.json`` (rewritten atomically after every
change), ``events.jsonl`` (append-only log) and ``report.md``. Completed
stages are stored by a unique, deterministic key so a resumed run can replay
them without calling the backend again.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

STATUSES = (
    "running",
    "awaiting_approval",
    "needs_human",
    "budget_exceeded",
    "failed",
    "done",
    "plan_only",
)


class RunStoreError(Exception):
    """Unknown run id or unreadable run state."""


class RunBusy(RunStoreError):
    """Another live ``carcara run`` holds ``.carcara/active.json``."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"another run is active: {run_id}")
        self.run_id = run_id


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, OSError):
        return False
    return True


def _proc_start(pid: int) -> str | None:
    """Start identity of process ``pid`` (guards against pid reuse); None if unknown."""
    try:
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
            # Field 22 (starttime); fields after the last ")" start at field 3.
            return raw.rsplit(")", 1)[1].split()[19]
        if sys.platform == "darwin":
            proc = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            return proc.stdout.strip() or None
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _holder_alive(data: dict[str, Any]) -> bool:
    """The lock holder is still the process that wrote the lock."""
    pid = data.get("pid")
    if not _pid_alive(pid):
        return False
    start = data.get("start")
    if not isinstance(start, str):
        return True  # no identity recorded: pid-only check
    current = _proc_start(pid)
    return current is None or current == start


# Leftovers of an interrupted ``acquire_lock`` next to ``active.json``.
LOCK_LEFTOVER_GLOBS = (".active.json.*.tmp", ".active.json.*.stale")


def _live_holder(text: str | None) -> dict[str, Any] | None:
    data = RunStore._parse_lock(text)
    if data is None or not _holder_alive(data):
        return None
    return data


def live_lock_holder(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The live holder of the lock file at ``path``; None if missing,
    unreadable, corrupt or stale. Only reads: never creates anything."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _live_holder(text)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _empty_totals() -> dict[str, Any]:
    return {"cost_usd": 0.0, "num_turns": 0, "input_tokens": 0, "output_tokens": 0}


class Run:
    """One run directory and its in-memory state."""

    def __init__(self, run_dir: Path, state: dict[str, Any]) -> None:
        self.dir = run_dir
        self.state = state

    @property
    def id(self) -> str:
        return self.state["run_id"]

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def save(self) -> None:
        self.state["updated_at"] = _now()
        tmp = self.dir / f".state.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def event(self, kind: str, **data: Any) -> None:
        record = {"ts": _now(), "event": kind, **data}
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def set_status(self, status: str, message: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown run status: {status}")
        self.state["status"] = status
        self.state["message"] = message
        self.save()
        self.event("status", status=status, message=message)

    def stage(self, key: str) -> dict[str, Any] | None:
        for entry in self.state["stages"]:
            if entry["key"] == key:
                return entry
        return None

    def add_cost(self, cost_usd: float, usage: dict[str, Any] | None, num_turns: int) -> None:
        totals = self.state["totals"]
        totals["cost_usd"] = round(totals["cost_usd"] + float(cost_usd or 0.0), 6)
        totals["num_turns"] += int(num_turns or 0)
        for name in ("input_tokens", "output_tokens"):
            value = (usage or {}).get(name)
            if isinstance(value, int):
                totals[name] += value

    def record_failed_attempt(
        self,
        key: str,
        stage: str,
        role: str | None,
        model: str,
        error: str,
        cost_usd: float | None,
        usage: dict[str, Any] | None,
        num_turns: int,
        counted: bool,
    ) -> None:
        """Remember an errored stage attempt; its cost (if known) is added separately."""
        self.state.setdefault("failed_attempts", []).append(
            {
                "key": key,
                "stage": stage,
                "role": role,
                "model": model,
                "error": error if len(error) <= 500 else error[:497] + "...",
                "cost_usd": cost_usd if counted else None,
                "usage": usage,
                "num_turns": num_turns,
                "counted": counted,
            }
        )
        if not counted:
            totals = self.state["totals"]
            totals["uncounted_stages"] = int(totals.get("uncounted_stages", 0)) + 1
        self.save()

    def record_stage(
        self,
        key: str,
        stage: str,
        role: str | None,
        model: str,
        output: Any,
        cost_usd: float,
        usage: dict[str, Any] | None,
        num_turns: int,
        session_id: str | None,
    ) -> None:
        if self.stage(key) is not None:
            raise RunStoreError(f"stage key already recorded: {key}")
        self.state["stages"].append(
            {
                "key": key,
                "stage": stage,
                "role": role,
                "model": model,
                "output": output,
                "cost_usd": cost_usd,
                "usage": usage,
                "num_turns": num_turns,
                "session_id": session_id,
            }
        )
        self.save()

    def amend_stage_output(self, key: str, output: Any) -> None:
        """Replace a recorded stage's output, keeping the first original once."""
        entry = self.stage(key)
        if entry is None:
            raise RunStoreError(f"stage key not recorded: {key}")
        entry.setdefault("original_output", entry["output"])
        entry["output"] = output
        self.save()

    def write_report(self, text: str) -> None:
        (self.dir / "report.md").write_text(text if text.endswith("\n") else text + "\n")

    def read_report(self) -> str | None:
        path = self.dir / "report.md"
        return path.read_text(encoding="utf-8") if path.is_file() else None


class RunStore:
    """Creates, loads and lists runs under ``<cwd>/.carcara/runs``."""

    def __init__(self, cwd: str | os.PathLike[str]) -> None:
        self.cwd = Path(cwd)
        self.base = self.cwd / ".carcara"
        self.root = self.base / "runs"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        ignore = self.base / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")

    def create(self, task: str, profile: str, base_sha: str, size: str | None = None) -> Run:
        self._ensure_root()
        for _ in range(100):
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
            run_dir = self.root / run_id
            try:
                run_dir.mkdir()
            except FileExistsError:
                continue
            break
        else:  # pragma: no cover - astronomically unlikely
            raise RunStoreError("could not allocate a run directory")
        state: dict[str, Any] = {
            "run_id": run_id,
            "task": task,
            "profile": profile,
            "size": size,
            "base_sha": base_sha,
            "status": "running",
            "message": None,
            "verify_round": 0,
            "plan_approved": False,
            "plan_revision": 0,
            "plan_feedback": [],
            "accepted_failures": None,
            "stages": [],
            "failed_attempts": [],
            "totals": _empty_totals(),
            "triage_range": None,
            "uncertainty_kind": None,
            "issue": None,
            "card_estimate": None,
            "plan_rejected": False,
            # Why review ran on a size that skips it by default (e.g. "verifiability gate").
            "review_reason": None,
            # Urutau record_run reporting; ``last`` becomes
            # {status, ok, code, claim_held, unverified_open, at}. Runs written
            # before this key existed read it with .get() (missing = disabled).
            "urutau": {
                "enabled": False,
                "repo": None,
                "issue": None,
                "sent_items": {},
                "last": None,
            },
            "created_at": _now(),
            "updated_at": _now(),
        }
        run = Run(run_dir, state)
        run.save()
        run.event("run_started", task=task, profile=profile, base_sha=base_sha)
        return run

    def load(self, run_id: str) -> Run:
        if not run_id or "/" in run_id or run_id in (".", ".."):
            raise RunStoreError(f"invalid run id: {run_id!r}")
        run_dir = self.root / run_id
        path = run_dir / "state.json"
        if not path.is_file():
            raise RunStoreError(f"unknown run: {run_id}")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"cannot read {path}: {exc}") from exc
        return Run(run_dir, state)

    # -- single active run lock ---------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.base / "active.json"

    def _read_lock_text(self) -> str | None:
        """The lock file's raw content, or None when missing/unreadable."""
        try:
            return self.lock_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None

    @staticmethod
    def _parse_lock(text: str | None) -> dict[str, Any] | None:
        try:
            data = json.loads(text) if text is not None else None
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("run_id"), str):
            return None
        return data

    def _read_lock(self) -> dict[str, Any] | None:
        """The lock's content, or None when missing or corrupt."""
        return self._parse_lock(self._read_lock_text())

    def active(self) -> dict[str, Any] | None:
        """The live lock holder ``{pid, run_id, started}``; None if absent or stale."""
        return _live_holder(self._read_lock_text())

    def acquire_lock(self, run_id: str) -> None:
        """Create ``active.json`` exclusively; take over stale/corrupt locks.

        Raises ``RunBusy`` when a live process holds the lock (even this one,
        under another run id).
        """
        self._ensure_root()
        lock: dict[str, Any] = {"pid": os.getpid(), "run_id": run_id, "started": _now()}
        start = _proc_start(os.getpid())
        if start is not None:
            lock["start"] = start
        payload = json.dumps(lock)
        token = f"{os.getpid()}.{secrets.token_hex(4)}"
        # Names must match LOCK_LEFTOVER_GLOBS.
        tmp = self.base / f".active.json.{token}.tmp"
        aside = self.base / f".active.json.{token}.stale"
        tmp.write_text(payload + "\n", encoding="utf-8")
        try:
            for _ in range(10):
                try:
                    self._create_lock(tmp, payload)
                    return
                except FileExistsError:
                    pass
                text = self._read_lock_text()
                if text is None and not self.lock_path.exists():
                    continue
                holder = self._parse_lock(text)
                if holder is not None and _holder_alive(holder):
                    raise RunBusy(holder["run_id"])
                self._take_over(text, aside)
            raise RunStoreError(f"could not acquire {self.lock_path}")
        finally:
            tmp.unlink(missing_ok=True)
            aside.unlink(missing_ok=True)

    def _take_over(self, stale_text: str | None, aside: Path) -> None:
        """Discard the lock judged stale from ``stale_text``.

        The lock is first moved aside atomically; if it no longer holds the
        content judged stale (another process replaced it in between), it is
        put back and ``RunBusy`` is raised.
        """
        try:
            os.rename(self.lock_path, aside)
        except FileNotFoundError:
            return
        try:
            moved = aside.read_text(encoding="utf-8")
        except OSError:
            moved = None
        if moved == stale_text:
            aside.unlink(missing_ok=True)
            return
        # Not the lock we judged stale: restore it unless the path was retaken.
        try:
            os.link(aside, self.lock_path)
        except FileExistsError:
            pass
        except OSError:
            if not self.lock_path.exists():
                os.rename(aside, self.lock_path)
        aside.unlink(missing_ok=True)
        holder = self._parse_lock(moved)
        raise RunBusy(holder["run_id"] if holder is not None else "unknown")

    def _create_lock(self, tmp: Path, payload: str) -> None:
        """Exclusive create; raises FileExistsError if the lock exists."""
        try:
            # Hard-linking a fully written file is atomic: readers never see
            # a partially written lock.
            os.link(tmp, self.lock_path)
        except FileExistsError:
            raise
        except OSError:
            # Filesystems without hard links: plain O_EXCL create.
            fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")

    def release_lock(self, run_id: str) -> None:
        """Remove the lock only if this process holds it for ``run_id``."""
        data = self._read_lock()
        if data is not None and data.get("run_id") == run_id and data.get("pid") == os.getpid():
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def list_runs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "state.json").is_file())
