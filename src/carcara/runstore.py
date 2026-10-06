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
            "accepted_failures": None,
            "stages": [],
            "totals": _empty_totals(),
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

    def list_runs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "state.json").is_file())
