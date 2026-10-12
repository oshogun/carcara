"""Tokens and active time of runs, per run and summarized across ``.carcara/runs``.

tokens = input + cache creation + cache reads + output, summed over every stage
and failed attempt. Active time is the union of the intervals while a stage was
running (stage_started to stage_completed/stage_error/stage_blocked), so parallel stages count
once and approval waits do not count. Dollar figures are API-price estimates,
not subscription billing.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from carcara.runstore import PAUSED_STATUSES, _parse_ts, _run_id_ts

COST_NOTE = "estimates at API prices; not subscription billing"
_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("cache_creation", "cache_creation_input_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("output", "output_tokens"),
)
_END_EVENTS = ("stage_completed", "stage_error", "stage_failed", "stage_blocked")
_UNFINISHED = ("running",) + PAUSED_STATUSES
_SIZE_ORDER = {"S": 0, "M": 1, "L": 2}
_MODE_ORDER = {"plain": 0, "ultra": 1, "triage": 2}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def stage_tokens(usage: dict[str, Any] | None) -> dict[str, int]:
    """Token breakdown of one stage's ``usage``; missing values count as 0."""
    usage = usage if isinstance(usage, dict) else {}
    out = {name: _int(usage.get(key)) for name, key in _TOKEN_KEYS}
    out["total"] = sum(out.values())
    return out


def _add(acc: dict[str, int], tokens: dict[str, int]) -> None:
    for name, value in tokens.items():
        acc[name] = acc.get(name, 0) + value


def _stage_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    stages = state.get("stages") or []
    stages = list(stages.values()) if isinstance(stages, dict) else list(stages)
    extra = list(state.get("failed_attempts") or []) + list(state.get("blocked_attempts") or [])
    return [e for e in stages + extra if isinstance(e, dict)]


def run_tokens(state: dict[str, Any]) -> dict[str, Any]:
    """Tokens summed over every recorded stage, failed and blocked attempt, plus a
    breakdown keyed by (role, model or '-'). A stage without a role is labelled
    by its stage name when a model ran it (e.g. triage), else 'deterministic'."""
    total = stage_tokens(None)
    by_role_model: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in _stage_entries(state):
        tokens = stage_tokens(entry.get("usage"))
        _add(total, tokens)
        model = entry.get("model")
        role = entry.get("role") or (model and entry.get("stage")) or "deterministic"
        key = (role, model or "-")
        group = by_role_model.setdefault(
            key, {"stages": 0, "est_cost_usd": 0.0, **stage_tokens(None)}
        )
        group["stages"] += 1
        group["est_cost_usd"] += float(entry.get("cost_usd") or 0.0)
        _add(group, tokens)
    return {**total, "by_role_model": by_role_model}


def read_events(run_dir: str | Path) -> list[dict[str, Any]]:
    """The run's events.jsonl records; [] when missing, bad lines skipped."""
    try:
        text = (Path(run_dir) / "events.jsonl").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    events = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events


def active_seconds(events: Iterable[dict[str, Any]], now: datetime | None = None) -> int:
    """Seconds while at least one stage was running. Stages still open are closed
    at ``now`` when given (a live run), else dropped (a crashed attempt)."""
    open_: dict[Any, datetime] = {}
    intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        kind = event.get("event")
        if kind != "stage_started" and kind not in _END_EVENTS:
            continue
        ts = _parse_ts(event.get("ts"))
        if ts is None:
            continue
        key = event.get("key")
        if kind == "stage_started":
            open_[key] = ts
        elif key in open_:
            intervals.append((open_.pop(key), ts))
    if now is not None:
        intervals += [(start, now) for start in open_.values()]
    total = 0.0
    end: datetime | None = None
    for start, stop in sorted(intervals):
        if end is not None and start < end:
            start = end
        if stop > start:
            total += (stop - start).total_seconds()
            end = stop
    return round(total)


def run_usage(
    run_or_state: Any, events: Iterable[dict[str, Any]], now: datetime | None = None
) -> dict[str, Any]:
    """Tokens, active time and the dollar estimate of one run."""
    state = getattr(run_or_state, "state", run_or_state) or {}
    tokens = run_tokens(state)
    return {
        "tokens": tokens["total"],
        "output_tokens": tokens["output"],
        "input_tokens": tokens["input"],
        "cache_creation_tokens": tokens["cache_creation"],
        "cache_read_tokens": tokens["cache_read"],
        "active_seconds": active_seconds(events, now),
        "est_cost_usd": float((state.get("totals") or {}).get("cost_usd") or 0.0),
    }


def format_duration(seconds: float) -> str:
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{round(n)}"


def usage_line(u: dict[str, Any]) -> str:
    return (
        f"usage: {format_tokens(u['tokens'])} tokens ({format_tokens(u['output_tokens'])} output), "
        f"active {format_duration(u['active_seconds'])}"
    )


def _run_date(run_id: str, state: dict[str, Any]) -> datetime | None:
    return _parse_ts(state.get("created_at")) or _run_id_ts(run_id)


def _mode(state: dict[str, Any]) -> str:
    if state.get("triage_only"):
        return "triage"
    return "ultra" if state.get("ultra") else "plain"


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"median": None, "min": None, "max": None, "total": 0}
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "total": sum(values),
    }


def summarize(
    entries: Iterable[tuple[str, dict[str, Any] | None, list[dict[str, Any]]]],
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate (run_id, state, events) entries, oldest first. ``since`` is
    inclusive and ``until`` exclusive, on each run's creation time. Medians and
    ranges cover finished runs only (not running or paused)."""
    totals = {"tokens": 0, "output_tokens": 0, "active_seconds": 0, "est_cost_usd": 0.0}
    outcomes: dict[str, int] = {}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    role_models: dict[tuple[str, str], dict[str, Any]] = {}
    runs = unreadable = 0
    for run_id, state, events in entries:
        if state is None:
            unreadable += 1
            continue
        date = _run_date(run_id, state)
        if (since is not None or until is not None) and date is None:
            continue
        if since is not None and date < since:
            continue
        if until is not None and date >= until:
            continue
        runs += 1
        status = str(state.get("status") or "?")
        u = run_usage(state, events)
        for name in totals:
            totals[name] += u[name]
        outcomes[status] = outcomes.get(status, 0) + 1
        key = (str(state.get("size") or "?"), _mode(state))
        group = groups.setdefault(key, {"runs": 0, "outcomes": {}, "finished": [], "cost": 0.0})
        group["runs"] += 1
        group["outcomes"][status] = group["outcomes"].get(status, 0) + 1
        group["cost"] += u["est_cost_usd"]
        if status not in _UNFINISHED:
            group["finished"].append(u)
        for (role, model), rm in run_tokens(state)["by_role_model"].items():
            acc = role_models.setdefault(
                (role, model),
                {"stages": 0, "tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0},
            )
            acc["stages"] += rm["stages"]
            acc["tokens"] += rm["total"]
            acc["output_tokens"] += rm["output"]
            acc["est_cost_usd"] += rm["est_cost_usd"]
    by_size_mode = []
    for (size, mode), group in sorted(
        groups.items(),
        key=lambda kv: (_MODE_ORDER.get(kv[0][1], 9), _SIZE_ORDER.get(kv[0][0], 9), kv[0]),
    ):
        finished = group["finished"]
        by_size_mode.append(
            {
                "size": size,
                "mode": mode,
                "runs": group["runs"],
                "finished": len(finished),
                "outcomes": group["outcomes"],
                **{
                    name: _stats([u[name] for u in finished])
                    for name in ("tokens", "output_tokens", "active_seconds")
                },
                "est_cost_usd_total": round(group["cost"], 6),
            }
        )
    by_role_model = [
        {"role": role, "model": model, **acc, "est_cost_usd": round(acc["est_cost_usd"], 6)}
        for (role, model), acc in sorted(role_models.items(), key=lambda kv: -kv[1]["tokens"])
    ]
    totals["est_cost_usd"] = round(totals["est_cost_usd"], 6)
    return {
        "runs": runs,
        "unreadable": unreadable,
        "range": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "totals": totals,
        "outcomes": outcomes,
        "by_size_mode": by_size_mode,
        "by_role_model": by_role_model,
    }


def _fmt_stat(stat: dict[str, Any], fmt: Any) -> str:
    if stat["median"] is None:
        return "-"
    return f"{fmt(stat['median'])} ({fmt(stat['min'])}-{fmt(stat['max'])})"


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows
    ]


def format_summary(summary: dict[str, Any], show_cost: bool = False) -> str:
    """Text tables: tokens and active time first; dollars only with ``show_cost``."""
    totals = summary["totals"]
    rng = summary["range"]
    span = ""
    if rng["since"] or rng["until"]:
        span = f" ({rng['since'] or '...'} to {rng['until'] or 'now'})"
    lines = [
        f"runs: {summary['runs']}{span}"
        + (f", {summary['unreadable']} unreadable" if summary["unreadable"] else ""),
        f"total: {format_tokens(totals['tokens'])} tokens "
        f"({format_tokens(totals['output_tokens'])} output), "
        f"active {format_duration(totals['active_seconds'])}",
    ]
    if summary["outcomes"]:
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(summary["outcomes"].items()))
        lines.append(f"outcomes: {outcomes}")
    if summary["by_size_mode"]:
        header = [
            "size",
            "mode",
            "runs",
            "tokens median (range)",
            "output median",
            "active median (range)",
        ]
        if show_cost:
            header.append("est. cost")
        rows = [header]
        for g in summary["by_size_mode"]:
            row = [
                g["size"],
                g["mode"],
                f"{g['runs']} ({g['finished']} finished)",
                _fmt_stat(g["tokens"], format_tokens),
                "-"
                if g["output_tokens"]["median"] is None
                else format_tokens(g["output_tokens"]["median"]),
                _fmt_stat(g["active_seconds"], format_duration),
            ]
            if show_cost:
                row.append(f"${g['est_cost_usd_total']:.2f}")
            rows.append(row)
        lines += ["", "by size and mode (median and range over finished runs):", *_table(rows)]
    if summary["by_role_model"]:
        header = ["role", "model", "stages", "tokens", "output"]
        if show_cost:
            header.append("est. cost")
        rows = [header]
        for rm in summary["by_role_model"]:
            row = [
                rm["role"],
                rm["model"],
                str(rm["stages"]),
                format_tokens(rm["tokens"]),
                format_tokens(rm["output_tokens"]),
            ]
            if show_cost:
                row.append(f"${rm['est_cost_usd']:.2f}")
            rows.append(row)
        lines += ["", "by role and model:", *_table(rows)]
    if show_cost:
        lines += [
            "",
            "est. cost (API-price estimate, not subscription billing): "
            f"${totals['est_cost_usd']:.2f}",
        ]
    return "\n".join(lines) + "\n"
