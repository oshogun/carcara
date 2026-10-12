from datetime import datetime, timedelta, timezone

from carcara.usage import (
    active_seconds,
    format_duration,
    format_summary,
    format_tokens,
    read_events,
    run_tokens,
    run_usage,
    stage_tokens,
    summarize,
    usage_line,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ts(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S%z")


def ev(seconds, kind, key=None, **extra):
    return {"ts": ts(seconds), "event": kind, "key": key, **extra}


def test_stage_tokens_sums_cache():
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 5000,
        "output_tokens": 30,
        "iterations": [{"input_tokens": 999}],
    }
    out = stage_tokens(usage)
    assert out == {
        "input": 10,
        "cache_creation": 200,
        "cache_read": 5000,
        "output": 30,
        "total": 5240,
    }
    assert stage_tokens({"input_tokens": None, "output_tokens": 4})["total"] == 4
    assert stage_tokens(None)["total"] == 0
    assert stage_tokens({"output_tokens": "x"})["total"] == 0


def test_run_tokens_includes_failed_attempts_and_groups():
    state = {
        "stages": [
            {
                "role": "implementer",
                "model": "opus",
                "cost_usd": 0.5,
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            {"role": None, "model": None, "cost_usd": 0.0, "usage": {"output_tokens": 3}},
        ],
        "failed_attempts": [
            {
                "role": "implementer",
                "model": "opus",
                "cost_usd": None,
                "usage": {"cache_read_input_tokens": 100},
            },
        ],
    }
    out = run_tokens(state)
    assert out["total"] == 106 and out["output"] == 5 and out["cache_read"] == 100
    impl = out["by_role_model"][("implementer", "opus")]
    assert impl["stages"] == 2 and impl["total"] == 103 and impl["est_cost_usd"] == 0.5
    assert out["by_role_model"][("deterministic", "-")]["total"] == 3
    triage = {"stage": "triage", "role": None, "model": "opus", "usage": {"output_tokens": 1}}
    assert ("triage", "opus") in run_tokens({"stages": [triage]})["by_role_model"]


def test_active_seconds_sequential():
    events = [
        ev(0, "stage_started", "a"),
        ev(60, "stage_completed", "a"),
        ev(70, "stage_started", "b"),
        ev(100, "stage_completed", "b"),
    ]
    assert active_seconds(events) == 90


def test_active_seconds_parallel_overlap():
    events = [
        ev(0, "stage_started", "explore:a"),
        ev(30, "stage_started", "explore:b"),
        ev(60, "stage_completed", "explore:a"),
        ev(90, "stage_completed", "explore:b"),
    ]
    assert active_seconds(events) == 90


def test_active_seconds_excludes_approval_pause():
    events = [
        ev(0, "stage_started", "plan"),
        ev(60, "stage_completed", "plan"),
        ev(61, "status", status="awaiting_approval"),
        ev(3600, "resumed"),
        ev(3660, "stage_started", "implement"),
        ev(3720, "stage_completed", "implement"),
    ]
    assert active_seconds(events) == 120


def test_active_seconds_errors_retries_and_open_intervals():
    events = [
        ev(0, "stage_started", "test"),
        ev(10, "stage_error", "test"),
        ev(20, "stage_started", "test"),
        ev(50, "stage_completed", "test"),
        ev(100, "stage_started", "review"),  # never closed
    ]
    assert active_seconds(events) == 40
    assert active_seconds(events, now=T0 + timedelta(seconds=130)) == 70
    # A crashed attempt restarted later: the restart replaces the unclosed start.
    crashed = [
        ev(0, "stage_started", "x"),
        ev(500, "stage_started", "x"),
        ev(510, "stage_failed", "x"),
    ]
    assert active_seconds(crashed) == 10


def test_active_seconds_blocked_implement_excludes_human_pause():
    events = [
        ev(0, "stage_started", "implement"),
        ev(40, "stage_blocked", "implement"),
        ev(41, "status", status="needs_human"),
        ev(3600, "resumed"),
        ev(3600, "stage_started", "implement"),
        ev(3650, "stage_completed", "implement"),
    ]
    assert active_seconds(events) == 90
    # While blocked, a live run does not count the human pause.
    assert active_seconds(events[:3], now=T0 + timedelta(seconds=3000)) == 40


def test_run_tokens_includes_blocked_attempts():
    state = {
        "stages": [{"role": "implementer", "model": "opus", "usage": {"output_tokens": 2}}],
        "blocked_attempts": [
            {"role": "implementer", "model": "opus", "usage": {"cache_read_input_tokens": 50}}
        ],
    }
    out = run_tokens(state)
    assert out["total"] == 52 and out["by_role_model"][("implementer", "opus")]["stages"] == 2


def test_read_events_tolerates_bad_lines(tmp_path):
    assert read_events(tmp_path) == []
    (tmp_path / "events.jsonl").write_text('{"event": "a"}\nnot json\n[1]\n{"event": "b"}\n')
    assert [e["event"] for e in read_events(tmp_path)] == ["a", "b"]


def test_run_usage_and_formatting():
    state = {
        "stages": [{"usage": {"input_tokens": 1_200_000, "output_tokens": 45_600}}],
        "totals": {"cost_usd": 1.5},
    }
    events = [ev(0, "stage_started", "a"), ev(754, "stage_completed", "a")]
    u = run_usage(state, events)
    assert u["tokens"] == 1_245_600 and u["est_cost_usd"] == 1.5
    assert usage_line(u) == "usage: 1.25M tokens (45.6k output), active 12m34s"
    assert format_duration(3720) == "1h02m" and format_duration(45) == "45s"
    assert format_tokens(812) == "812" and format_tokens(456_700) == "456.7k"


def _state(status, size, tokens, *, ultra=False, triage_only=False, created=0, active=0):
    return {
        "status": status,
        "size": size,
        "ultra": ultra,
        "triage_only": triage_only,
        "created_at": ts(created),
        "stages": [
            {
                "role": "implementer",
                "model": "opus",
                "cost_usd": 0.1,
                "usage": {"input_tokens": tokens - 10, "output_tokens": 10},
            }
        ],
        "totals": {"cost_usd": 0.1},
    }, [ev(created, "stage_started", "s"), ev(created + active, "stage_completed", "s")]


def _entry(run_id, *args, **kwargs):
    state, events = _state(*args, **kwargs)
    return run_id, state, events


def test_summarize_groups_and_stats():
    entries = [
        _entry("r1", "done", "S", 100, active=10),
        _entry("r2", "done", "S", 300, active=30),
        _entry("r3", "failed", "S", 200, active=20),
        _entry("r4", "running", "S", 5000, active=500),
        _entry("r5", "done", "S", 1000, ultra=True),
        _entry("r6", "triaged", None, 50, triage_only=True),
        _entry("r7", "done", "L", 900),
        ("r8", None, []),
    ]
    out = summarize(entries)
    assert out["runs"] == 7 and out["unreadable"] == 1
    assert out["outcomes"] == {"done": 4, "failed": 1, "running": 1, "triaged": 1}
    assert out["totals"]["tokens"] == 100 + 300 + 200 + 5000 + 1000 + 50 + 900
    assert out["totals"]["active_seconds"] == 560
    groups = {(g["size"], g["mode"]): g for g in out["by_size_mode"]}
    assert [(g["size"], g["mode"]) for g in out["by_size_mode"]] == [
        ("S", "plain"),
        ("L", "plain"),
        ("S", "ultra"),
        ("?", "triage"),
    ]
    s = groups[("S", "plain")]
    assert s["runs"] == 4 and s["finished"] == 3  # the running run is counted, not in stats
    assert s["tokens"] == {"median": 200, "min": 100, "max": 300, "total": 600}
    assert s["active_seconds"]["median"] == 20
    assert s["outcomes"] == {"done": 2, "failed": 1, "running": 1}
    (rm,) = out["by_role_model"]
    assert rm["role"] == "implementer" and rm["stages"] == 7 and rm["output_tokens"] == 70


def test_summarize_date_filter_and_run_id_fallback():
    old = _entry("r1", "done", "S", 100, created=0)
    new = _entry("r2", "done", "S", 200, created=86400 * 10)
    legacy_state, legacy_events = _state("done", "M", 300)
    del legacy_state["created_at"]
    legacy = ("20260105-120000-abcd", legacy_state, legacy_events)
    since = T0 + timedelta(days=1)
    out = summarize([old, new, legacy], since=since)
    assert out["runs"] == 2 and out["totals"]["tokens"] == 500
    assert out["range"]["since"] == since.isoformat()
    out = summarize([old, new, legacy], until=T0 + timedelta(days=10))
    assert out["runs"] == 2 and out["totals"]["tokens"] == 400  # until is exclusive


def test_format_summary_cost_only_on_request():
    out = summarize([_entry("r1", "done", "S", 100, active=65)])
    text = format_summary(out)
    assert "$" not in text and "100 tokens" in text and "1m05s" in text
    costly = format_summary(out, show_cost=True)
    assert "est. cost (API-price estimate, not subscription billing): $0.10" in costly
