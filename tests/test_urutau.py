"""Urutau record_run client and payload helpers (no network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from carcara import urutau
from carcara.urutau import (
    ClaimConflict,
    SetupError,
    ToolReply,
    UrutauClient,
    UrutauConfig,
    UrutauError,
    build_inventory,
    filter_areas,
    filter_files,
    load_config,
)

FIX = Path(__file__).parent / "fixtures" / "urutau"
TOKEN = "tok-SECRET-123"


def sample(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())


def ok(data: dict[str, Any]) -> ToolReply:
    return ToolReply(is_error=False, data=data, text=json.dumps(data))


def err(data: dict[str, Any]) -> ToolReply:
    return ToolReply(is_error=True, data=data, text=json.dumps(data))


class FakeTransport:
    """Pops scripted replies; records (tool, args) in a shared log."""

    def __init__(self, script: list[ToolReply], log: list[tuple[str, dict]], opened: list[int]):
        self.script, self.log = script, log
        opened.append(1)

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolReply:
        self.log.append((name, dict(args)))
        return self.script.pop(0)

    async def aclose(self) -> None:
        pass


def make_client(script: list[ToolReply]):
    log: list[tuple[str, dict]] = []
    opened: list[int] = []
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    client = UrutauClient(
        UrutauConfig("http://127.0.0.1:8787", TOKEN),
        "acme/widgets",
        7,
        transport_factory=lambda cfg: FakeTransport(script, log, opened),
        sleep=sleep,
    )
    return client, log, opened, slept


def run(coro):
    return asyncio.run(coro)


# --- config ---


def test_load_config_env_wins_and_redacts(tmp_path):
    cfg_dir = tmp_path / "carcara"
    cfg_dir.mkdir()
    (cfg_dir / "urutau.json").write_text(json.dumps({"url": "http://file:1/", "token": "file-tok"}))
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    cfg = load_config(env)
    assert cfg == UrutauConfig("http://file:1", "file-tok")
    cfg = load_config({**env, "URUTAU_MCP_TOKEN": TOKEN, "URUTAU_URL": "http://env:2/"})
    assert cfg is not None and cfg.base_url == "http://env:2" and cfg.token == TOKEN
    assert TOKEN not in repr(cfg) and TOKEN not in str(cfg)


def test_load_config_without_token(tmp_path):
    assert load_config({"XDG_CONFIG_HOME": str(tmp_path)}) is None
    cfg = load_config({"XDG_CONFIG_HOME": str(tmp_path), "URUTAU_MCP_TOKEN": TOKEN})
    assert cfg is not None and cfg.base_url == urutau.DEFAULT_URL


# --- files / areas ---


def test_filter_files():
    paths = ["src/a.py", ".github/workflows/ci.yml", "./x", "a/../b", "a b", "x" * 257]
    assert filter_files(paths) == (["src/a.py", ".github/workflows/ci.yml"], 4)
    many = [f"f{i}.py" for i in range(205)]
    files, omitted = filter_files(many + ["bad path"])
    assert len(files) == 200 and omitted == 6


def test_filter_areas():
    areas = [f"a{i}" for i in range(12)] + ["x" * 65, "a/./b"]
    assert filter_areas(areas) == [f"a{i}" for i in range(10)]


# --- inventory ---


def _state(items, results=None, sent=None):
    return {
        "unverified": items,
        "probe_results": results or {},
        "urutau": {"sent_items": sent or {}},
    }


ITEMS = [
    {"id": "U1", "kind": "external", "text": "ext"},
    {"id": "U2", "kind": "normative", "text": "norm"},
    {"id": "U3", "kind": "untested", "text": "unt"},
]


def test_inventory_first_send_and_unchanged():
    unv, wd, probes, findings, sent = build_inventory(_state(ITEMS))
    assert [i["id"] for i in unv] == ["U1", "U2", "U3"]
    assert unv[0] == {"id": "U1", "kind": "external", "text": "ext"}
    assert (wd, probes, findings) == ([], [], [])
    again = build_inventory(_state(ITEMS, sent=sent))
    assert again[:4] == ([], [], [], [])


def test_inventory_reworded_items():
    state = _state(ITEMS)
    sent = build_inventory(state)[4]
    items = [
        {"id": "U1", "kind": "external", "text": "ext v2"},
        {"id": "U2", "kind": "normative", "text": "norm v2"},
        ITEMS[2],
    ]
    results = {"U3": {"probe": {"name": "p"}, "outcome": "inconclusive", "result": "HTTP 500"}}
    state = _state(items, results=results, sent=sent)
    before = json.dumps(state, sort_keys=True)
    unv, wd, probes, findings, new_sent = build_inventory(state)
    assert json.dumps(state, sort_keys=True) == before  # nothing mutated
    assert unv == [
        {"id": "U1-r1", "kind": "external", "text": "ext v2"},
        {"id": "U2-r1", "kind": "normative", "text": "norm v2"},
    ]
    assert wd == ["U1"]  # normative U2 is never withdrawn
    assert "normative item U2 reworded as U2-r1" in findings
    assert new_sent["U1"]["withdrawn"] and not new_sent["U2"]["withdrawn"]
    # A second rewording continues the series.
    items[0] = {"id": "U1", "kind": "external", "text": "ext v3"}
    unv, wd, *_ = build_inventory(_state(items, sent=new_sent))
    assert unv == [{"id": "U1-r2", "kind": "external", "text": "ext v3"}] and wd == ["U1-r1"]


def test_inventory_gone_items():
    sent = build_inventory(_state(ITEMS))[4]
    unv, wd, probes, findings, _ = build_inventory(_state([], sent=sent))
    assert unv == [] and wd == ["U1", "U3"]
    assert any("U2" in f for f in findings)


def test_inventory_probes():
    results = {
        "U1": {
            "probe": {"name": "gh", "arg": "x", "expect": "exists"},
            "outcome": "confirmed",
            "result": "HTTP 200",
        },
        "U3": {
            "probe": {"name": "api", "arg": "y", "expect": "absent"},
            "outcome": "contradicted",
            "result": "HTTP 200",
        },
    }
    unv, wd, probes, findings, _ = build_inventory(_state(ITEMS, results=results))
    assert probes == [{"item": "U1", "note": "probe gh: HTTP 200 (expect exists)"}]
    assert findings == ["probe api on U3: contradicted, HTTP 200 (expect absent)"]
    assert not {p["item"] for p in probes} & set(wd)


def test_inventory_cap():
    items = [{"id": f"U{i}", "kind": "external", "text": f"t{i}"} for i in range(1, 23)]
    unv, _, _, findings, sent = build_inventory(_state(items))
    assert len(unv) == 20 and len(sent) == 20
    assert any("not sent" in f for f in findings)


# --- client ---


def test_record_run_ok():
    client, log, _, _ = make_client([ok(sample("record_run-start.output.json"))])
    res = run(client.record_run(sample("record_run-start.input.json")))
    assert res.ok and res.claim_held is True
    assert res.unverified_open == {"external": 1, "normative": 1, "untested": 1}
    assert log[0][0] == "record_run"


def test_rate_limited_waits_and_retries_once():
    limited = err({"error": "rate-limited", "message": "slow", "retryAfterSeconds": 2})
    client, log, _, slept = make_client([limited, ok(sample("record_run-heartbeat.output.json"))])
    assert run(client.record_run(sample("record_run-heartbeat.input.json"))).ok
    assert slept == [2] and len(log) == 2
    client, log, _, slept = make_client([limited, limited])
    with pytest.raises(UrutauError) as e:
        run(client.record_run(sample("record_run-heartbeat.input.json")))
    assert e.value.code == "rate-limited" and len(log) == 2


def test_server_error_retries_once():
    boom = err({"error": "server-error", "message": "x"})
    client, log, _, _ = make_client([boom, ok(sample("record_run-heartbeat.output.json"))])
    assert run(client.record_run(sample("record_run-heartbeat.input.json"))).ok
    assert len(log) == 2


def test_run_finished():
    finished = sample("record_run-run-finished.error.json")
    client, _, _, _ = make_client([err(finished)])
    res = run(client.record_run({**sample("record_run-heartbeat.input.json"), "status": "done"}))
    assert res.ok and res.code == "run-finished" and res.claim_held is False
    client, _, _, _ = make_client([err(finished)])
    with pytest.raises(UrutauError) as e:
        run(client.record_run({**sample("record_run-heartbeat.input.json"), "status": "failed"}))
    assert e.value.code == "run-finished" and not isinstance(e.value, SetupError)


def test_claimed_by_other_run():
    client, _, _, _ = make_client([err(sample("record_run-claimed-by-other-run.error.json"))])
    with pytest.raises(ClaimConflict):
        run(client.record_run(sample("record_run-start.input.json")))


@pytest.mark.parametrize(
    "code,hint",
    [("no-board", "open a board for acme/widgets"), ("repo-not-allowed", "add acme/widgets")],
)
def test_setup_errors(code, hint):
    client, _, _, _ = make_client([err({"error": code, "message": "m"})])
    with pytest.raises(SetupError) as e:
        run(client.record_run(sample("record_run-start.input.json")))
    assert e.value.code == code and hint in e.value.message


def test_schema_error_retries_once():
    # Each transport call is its own MCP session, so a retry is a fresh session.
    plain = ToolReply(is_error=True, data=None, text="Input validation error: 'kind' unexpected")
    client, log, _, _ = make_client([plain, ok(sample("record_run-start.output.json"))])
    assert run(client.record_run(sample("record_run-start.input.json"))).ok
    assert len(log) == 2
    client, log, _, _ = make_client([plain, plain])
    with pytest.raises(UrutauError) as e:
        run(client.record_run(sample("record_run-start.input.json")))
    assert e.value.code == "schema" and len(log) == 2


def test_request_guard_only_allows_base_mcp():
    httpx2 = pytest.importorskip("httpx2")

    def check(base: str, url: str) -> bool:
        try:
            run(urutau.make_request_guard(base)(httpx2.Request("POST", url)))
        except urutau.TokenScopeError as exc:
            assert exc.code == "token-scope" and TOKEN not in str(exc)
            return False
        return True

    base = "http://127.0.0.1:8787"
    assert check(base, f"{base}/mcp")
    assert check("https://u.example/api/", "https://u.example:443/api/mcp")
    assert not check(base, f"{base}/other")
    assert not check(base, f"{base}/mcp/")
    assert not check(base, "http://127.0.0.1:9999/mcp")
    assert not check(base, "http://evil.example:8787/mcp")
    assert not check(base, "https://127.0.0.1:8787/mcp")  # a redirect to another scheme
    assert not check("https://u.example/api", "https://u.example/mcp")


def test_mcp_http_client_does_not_follow_redirects():
    pytest.importorskip("mcp")
    transport = urutau.McpTransport(UrutauConfig("http://127.0.0.1:8787", TOKEN))
    seen: dict[str, Any] = {}
    http = transport._http_client(seen)
    try:
        assert http.follow_redirects is False
        assert len(http.event_hooks["request"]) == 1
        httpx2 = pytest.importorskip("httpx2")
        hook = http.event_hooks["request"][0]
        run(hook(httpx2.Request("POST", "http://127.0.0.1:8787/mcp")))
        with pytest.raises(urutau.TokenScopeError):
            run(hook(httpx2.Request("POST", "http://127.0.0.1:8787/elsewhere")))
        assert isinstance(seen["refused"], urutau.TokenScopeError)
    finally:
        run(http.aclose())


def bucket(id: str, cards: list[dict[str, Any]], more: bool = False, offset: int = 0) -> dict:
    total = offset + len(cards) + (1 if more else 0)
    return {"id": id, "cards": cards, "total": total, "offset": offset, "more": more}


def board(*buckets: dict) -> dict[str, Any]:
    return {**sample("get_board-top-level.json"), "buckets": list(buckets)}


def assert_offset_has_one_bucket(log: list[tuple[str, dict]]) -> None:
    for _, args in log:
        if "offset" in args:
            assert len(args.get("buckets", [])) == 1


def test_get_estimate():
    card = sample("get_board-card.json")
    other = {**card, "number": 3}
    client, log, _, _ = make_client([ok(board(bucket("todo", [other]), bucket("doing", [card])))])
    assert run(client.get_estimate()) == {"size": "S", "confidence": "unsure"}
    assert log == [("get_board", {"repo": "acme/widgets", "includeClosed": True})]


def test_get_estimate_card_without_estimate_stops_paging():
    card = {**sample("get_board-card.json"), "estimate": None}
    other = {**card, "number": 3}
    script = [ok(board(bucket("todo", [other], more=True), bucket("doing", [card])))]
    client, log, _, _ = make_client(script)
    assert run(client.get_estimate()) is None
    assert len(log) == 1
    assert_offset_has_one_bucket(log)


def test_get_estimate_pages_one_bucket_at_a_time():
    card = sample("get_board-card.json")
    other = {**card, "number": 3}
    first = board(bucket("todo", [other, {**card, "number": 4}], more=True), bucket("done", []))
    second = board(bucket("todo", [card], offset=2))
    client, log, _, _ = make_client([ok(first), ok(second)])
    assert run(client.get_estimate()) == {"size": "S", "confidence": "unsure"}
    assert len(log) == 2
    assert log[1] == (
        "get_board",
        {"repo": "acme/widgets", "includeClosed": True, "buckets": ["todo"], "offset": 2},
    )
    assert_offset_has_one_bucket(log)


def test_get_estimate_absent_card_is_none_and_bounded():
    other = {**sample("get_board-card.json"), "number": 3}
    pages = [ok(board(bucket("todo", [other], more=True), bucket("doing", [other], more=True)))]
    pages += [ok(board(bucket("todo", [other], more=True)))] * (urutau.MAX_BOARD_PAGES + 2)
    client, log, _, _ = make_client(pages)
    assert run(client.get_estimate()) is None
    assert len(log) == urutau.MAX_BOARD_PAGES
    assert_offset_has_one_bucket(log)
    assert [a["offset"] for _, a in log[1:3]] == [1, 2]


def test_errors_never_carry_the_token():
    client, _, _, _ = make_client(
        [ToolReply(True, None, f"bad {TOKEN}"), ToolReply(True, None, f"bad {TOKEN}")]
    )
    with pytest.raises(UrutauError) as e:
        run(client.record_run({}))
    assert TOKEN not in str(e.value) and TOKEN not in repr(client)


def test_urutau_status():
    assert urutau.urutau_status("failed", {"plan_rejected": True}) == "rejected"
    assert urutau.urutau_status("failed", {}) == "failed"
    for status in ("running", "done", "awaiting_approval", "needs_human", "plan_only"):
        assert urutau.urutau_status(status, {"plan_rejected": True}) == status
