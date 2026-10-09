"""Report ``carcara run --issue N`` runs to Urutau through its ``record_run`` MCP tool.

The contract is Urutau's design §18 (a copy lives in
``tests/fixtures/urutau/record_run-contract.md``). Each call opens its own
short-lived MCP session over streamable HTTP to ``<base>/mcp``. The token is
sent only in the ``Authorization`` header of requests to exactly
``<base>/mcp`` (redirects elsewhere are refused); it is never logged, stored
in run state, or put in an error message.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_URL = "http://127.0.0.1:8787"
TOKEN_HELP = "set URUTAU_MCP_TOKEN (Urutau Users page → Agent integrations)"

FILE_RE = re.compile(r"^[A-Za-z0-9._@+-]+(/[A-Za-z0-9._@+-]+)*$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
MAX_FILES = 200
MAX_FILE_CHARS = 256
MAX_AREAS = 10
MAX_AREA_CHARS = 64
MAX_ITEMS = 20
MAX_ITEM_TEXT = 1000
MAX_NOTE = 1000
CLOSABLE_KINDS = ("external", "untested")
MAX_BOARD_PAGES = 10


# --- config -----------------------------------------------------------------


@dataclass(frozen=True)
class UrutauConfig:
    base_url: str
    token: str = field(repr=False)

    def __repr__(self) -> str:
        return f"UrutauConfig(base_url={self.base_url!r}, token=<redacted>)"

    __str__ = __repr__


def _config_file(env: Mapping[str, str], home: Path | None) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (home or Path.home()) / ".config"
    return base / "carcara" / "urutau.json"


def load_config(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> UrutauConfig | None:
    """Env (URUTAU_URL, URUTAU_MCP_TOKEN) wins over the user file; None without a token."""
    env = os.environ if env is None else env
    data: dict[str, Any] = {}
    path = _config_file(env, home)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    token = (env.get("URUTAU_MCP_TOKEN") or str(data.get("token") or "")).strip()
    if not token:
        return None
    url = (env.get("URUTAU_URL") or str(data.get("url") or "") or DEFAULT_URL).strip()
    return UrutauConfig(base_url=url.rstrip("/") or DEFAULT_URL, token=token)


# --- errors -----------------------------------------------------------------


class UrutauError(Exception):
    """A record_run/get_board call failed; ``code`` is Urutau's error code or a local one."""

    def __init__(self, code: str, message: str = "", data: dict[str, Any] | None = None):
        self.code = code
        self.message = message or code
        self.data = data or {}
        super().__init__(f"Urutau {code}: {self.message}")


class ClaimConflict(UrutauError):
    """Another run holds the claim on this issue (claimed-by-other-run)."""


class SetupError(UrutauError):
    """Urutau cannot accept this run until a person changes something."""


def _setup_message(code: str, repo: str, server_message: str = "") -> str:
    hints = {
        "repo-not-allowed": f"ask the Urutau admin to add {repo} to the integration",
        "no-board": f"ask a person to open a board for {repo} in Urutau, then retry",
        "run-id-taken": "this run id is used by another issue or integration; start a new run",
        "call-stopped": f"the token was revoked or the integration removed; {TOKEN_HELP}",
        "auth": f"Urutau refused the token; {TOKEN_HELP}",
        "connection": "cannot reach Urutau; check URUTAU_URL and that the server is running, "
        "or pass --no-urutau",
    }
    hint = hints.get(code, "")
    return f"{server_message} ({hint})" if server_message and hint else server_message or hint


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


# --- transport --------------------------------------------------------------


@dataclass
class ToolReply:
    is_error: bool
    data: dict[str, Any] | None
    text: str


class Transport(Protocol):
    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolReply: ...

    async def aclose(self) -> None: ...


def _as_dict(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _walk_exc(exc: BaseException) -> list[BaseException]:
    out = [exc]
    for sub in getattr(exc, "exceptions", ()) or ():
        out.extend(_walk_exc(sub))
    return out


class TokenScopeError(UrutauError):
    """A request (e.g. a redirect) would have carried the token away from ``<base>/mcp``."""


def _origin(scheme: str, host: str | None, port: int | None) -> tuple[str, str, int | None]:
    default = {"http": 80, "https": 443}.get(scheme)
    return scheme, (host or "").lower(), None if port == default else port


def make_request_guard(base_url: str) -> Callable[[Any], Awaitable[None]]:
    """An httpx request hook refusing any request not for exactly ``<base>/mcp``.

    The token rides in the client's default headers, so this keeps it from
    following a redirect (the SDK follows same-origin ones) to another path.
    """
    from urllib.parse import urlsplit

    base = urlsplit(base_url)
    expected = _origin(base.scheme, base.hostname, base.port)
    expected_path = base.path.rstrip("/") + "/mcp"

    async def guard(request: Any) -> None:
        url = request.url
        if _origin(url.scheme, url.host, url.port) != expected or url.path != expected_path:
            raise TokenScopeError(
                "token-scope", "refused to send the Urutau token outside <base>/mcp"
            )

    return guard


class McpTransport:
    """MCP streamable-HTTP to ``<base>/mcp``: one short-lived session per call.

    Every call opens, uses and closes its own session inside the calling task,
    so no anyio task group or cancel scope is ever shared between tasks.
    """

    def __init__(self, config: UrutauConfig):
        self._config = config

    def _http_client(self, seen: dict[str, Any]) -> Any:
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(headers={"Authorization": f"Bearer {self._config.token}"})
        http.follow_redirects = False

        # The SDK reports HTTP errors without their status; keep it for _map_exc.
        async def note_status(response: Any) -> None:
            seen["status"] = response.status_code

        guard = make_request_guard(self._config.base_url)

        async def checked(request: Any) -> None:
            try:
                await guard(request)
            except TokenScopeError as exc:
                seen["refused"] = exc
                raise

        http.event_hooks["request"].append(checked)
        http.event_hooks["response"].append(note_status)
        return http

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolReply:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        seen: dict[str, Any] = {}
        try:
            async with self._http_client(seen) as http:
                async with streamable_http_client(
                    f"{self._config.base_url}/mcp", http_client=http
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - mapped to UrutauError without the token
            raise self._map_exc(exc, seen) from None
        if "refused" in seen:
            raise seen["refused"]
        text = "\n".join(
            getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", None)
        )
        data = result.structured_content
        if not isinstance(data, dict):
            data = _as_dict(text)
        return ToolReply(is_error=bool(result.is_error), data=data, text=text)

    def _map_exc(self, exc: Exception, seen: Mapping[str, Any]) -> UrutauError:
        token = self._config.token
        parts = _walk_exc(exc)
        if isinstance(seen.get("refused"), UrutauError):
            return seen["refused"]
        for e in parts:
            if isinstance(e, UrutauError):
                return e
        detail = _scrub("; ".join(f"{type(e).__name__}: {e}" for e in parts[-3:]), token)
        if seen.get("status") in (401, 403):
            return SetupError("auth", _setup_message("auth", ""), {"detail": detail})
        for e in parts:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403) or re.search(r"\b40[13]\b", str(e)):
                return SetupError("auth", _setup_message("auth", ""), {"detail": detail})
            if isinstance(e, (ConnectionError, OSError)) or "ConnectError" in type(e).__name__:
                return SetupError(
                    "connection", _setup_message("connection", ""), {"detail": detail}
                )
        return UrutauError("transport", detail)

    async def aclose(self) -> None:
        """Nothing to close: each call's session is closed when the call ends."""


# --- client -----------------------------------------------------------------


@dataclass
class RecordResult:
    ok: bool
    code: str | None = None
    claim_held: bool | None = None
    unverified_open: dict[str, int] | None = None
    message: str = ""


_SETUP_CODES = ("repo-not-allowed", "no-board", "run-id-taken", "call-stopped")


class UrutauClient:
    """record_run/get_board for one repo and issue; one transport per carcara invocation."""

    def __init__(
        self,
        config: UrutauConfig,
        repo: str,
        issue: int,
        transport_factory: Callable[[UrutauConfig], Transport] = McpTransport,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.config = config
        self.repo = repo
        self.issue = issue
        self._factory = transport_factory
        self._sleep = sleep
        self._transport: Transport | None = None

    def __repr__(self) -> str:
        return (
            f"UrutauClient(base_url={self.config.base_url!r}, "
            f"repo={self.repo!r}, issue={self.issue})"
        )

    def _get_transport(self) -> Transport:
        if self._transport is None:
            self._transport = self._factory(self.config)
        return self._transport

    async def aclose(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.aclose()

    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call a tool with §18's retry rules; return its result data or raise UrutauError."""
        retried: set[str] = set()
        while True:
            reply = await self._get_transport().call_tool(name, args)
            if not reply.is_error:
                return reply.data or {}
            err = reply.data if reply.data and isinstance(reply.data.get("error"), str) else None
            if err is None:
                # Schema violation from the MCP library: maybe stale schemas after a
                # redeploy. Retry once; each call already opens a fresh session.
                text = _scrub(reply.text, self.config.token)
                if "schema" in retried:
                    raise UrutauError("schema", text)
                retried.add("schema")
                continue
            code = err["error"]
            message = _scrub(str(err.get("message") or ""), self.config.token)
            if code == "rate-limited" and code not in retried:
                retried.add(code)
                wait = err.get("retryAfterSeconds")
                await self._sleep(float(wait) if isinstance(wait, (int, float)) and wait > 0 else 1)
                continue
            if code == "server-error" and code not in retried:
                retried.add(code)
                continue
            if code == "run-finished" and err.get("runStatus") == args.get("status"):
                return {"runFinished": True, "claim": {"held": False}}
            if code == "claimed-by-other-run":
                raise ClaimConflict(code, message, err)
            if code in _SETUP_CODES:
                raise SetupError(code, _setup_message(code, self.repo, message), err)
            raise UrutauError(code, message, err)

    async def record_run(self, payload: dict[str, Any]) -> RecordResult:
        data = await self._call("record_run", payload)
        claim = data.get("claim")
        held = claim.get("held") if isinstance(claim, dict) else None
        open_ = data.get("unverifiedOpen")
        return RecordResult(
            ok=True,
            code="run-finished" if data.get("runFinished") else None,
            claim_held=bool(held) if held is not None else None,
            unverified_open=dict(open_) if isinstance(open_, dict) else None,
        )

    async def get_estimate(self) -> dict[str, Any] | None:
        """The card's {size, confidence} from get_board, or None when the issue has none."""
        offset = 0
        seen: set[int] = set()
        for _ in range(MAX_BOARD_PAGES):
            args: dict[str, Any] = {"repo": self.repo, "includeClosed": True}
            if offset:
                args["offset"] = offset
            data = await self._call("get_board", args)
            found = _find_card(data, self.issue)
            if found is not None:
                est = found["estimate"]
                return {"size": est.get("size"), "confidence": est.get("confidence")}
            lists = _card_lists(data)
            numbers = {c["number"] for cards in lists for c in cards}
            page = max((len(cards) for cards in lists), default=0)
            if not page or numbers <= seen:
                return None
            seen |= numbers
            offset += page
        return None


def _find_card(node: Any, issue: int) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if node.get("number") == issue and isinstance(node.get("estimate"), dict):
            return node
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            hit = _find_card(child, issue)
            if hit is not None:
                return hit
    return None


def _card_lists(node: Any) -> list[list[dict[str, Any]]]:
    """Every list of card-like dicts (with an int ``number``) in a get_board answer."""
    out: list[list[dict[str, Any]]] = []
    if isinstance(node, dict):
        for child in node.values():
            out.extend(_card_lists(child))
    elif isinstance(node, list):
        cards = [c for c in node if isinstance(c, dict) and isinstance(c.get("number"), int)]
        if cards:
            out.append(cards)
        for child in node:
            if isinstance(child, (dict, list)):
                out.extend(_card_lists(child))
    return out


# --- payload helpers --------------------------------------------------------


def valid_path(p: Any, maxlen: int = MAX_FILE_CHARS) -> bool:
    if not isinstance(p, str) or not p or len(p) > maxlen or not FILE_RE.match(p):
        return False
    return all(seg not in (".", "..") for seg in p.split("/"))


def filter_files(paths: Any) -> tuple[list[str], int]:
    """(up to 200 valid paths, count of paths left out: invalid plus overflow)."""
    valid: list[str] = []
    omitted = 0
    for p in paths or []:
        if valid_path(p) and p not in valid:
            valid.append(p)
        elif not valid_path(p):
            omitted += 1
    omitted += max(0, len(valid) - MAX_FILES)
    return valid[:MAX_FILES], omitted


def filter_areas(areas: Any) -> list[str]:
    out: list[str] = []
    for a in areas or []:
        if valid_path(a, MAX_AREA_CHARS) and a not in out:
            out.append(a)
    return out[:MAX_AREAS]


def urutau_status(run_status: str, state: Mapping[str, Any]) -> str:
    return "rejected" if run_status == "failed" and state.get("plan_rejected") else run_status


def _first_id(local_id: str, taken: Mapping[str, Any]) -> str:
    if ITEM_ID_RE.match(local_id) and local_id not in taken:
        return local_id
    return _reword_id(local_id, taken, 0)


def _reword_id(local_id: str, taken: Mapping[str, Any], earlier: int) -> str:
    digits = "".join(ch for ch in local_id if ch.isdigit())[:20] or "0"
    n = max(earlier, 1)  # the first rewording of U1 is U1-r1
    while f"U{digits}-r{n}" in taken:
        n += 1
    return f"U{digits}-r{n}"


def build_inventory(
    state: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]], list[str], dict[str, Any]]:
    """Map carcara's unverified items onto §18 ids.

    Returns (unverified, withdrawn, probes, findings_lines, new_sent). Ids
    Urutau already has never change kind or text: a reworded item gets a new
    id and its old external/untested id is withdrawn. ``new_sent`` replaces
    ``state['urutau']['sent_items']`` only after the call succeeds. ``state``
    is not mutated.
    """
    urutau = state.get("urutau") or {}
    sent: dict[str, dict[str, Any]] = {
        k: dict(v) for k, v in (urutau.get("sent_items") or {}).items()
    }
    items = [i for i in state.get("unverified") or [] if isinstance(i, dict)]
    results = state.get("probe_results") or {}
    unverified: list[dict[str, str]] = []
    withdrawn: list[str] = []
    findings: list[str] = []
    current: dict[str, str] = {}  # local id -> urutau id
    capped = 0

    for item in items:
        local = str(item.get("id", ""))
        kind = item.get("kind")
        text = str(item.get("text") or "").strip()[:MAX_ITEM_TEXT]
        if not local or not text or kind not in ("external", "normative", "untested"):
            continue
        entries = [k for k, v in sent.items() if v.get("local_id") == local]
        live = [k for k in entries if not sent[k].get("withdrawn")]
        old = live[-1] if live else None
        if old is not None and sent[old]["kind"] == kind and sent[old]["text"] == text:
            current[local] = old
            continue
        if len(sent) >= MAX_ITEMS:
            capped += 1
            if old is not None:
                current[local] = old
            continue
        new = _reword_id(local, sent, len(entries)) if entries else _first_id(local, sent)
        sent[new] = {"local_id": local, "kind": kind, "text": text, "withdrawn": False}
        unverified.append({"id": new, "kind": kind, "text": text})
        current[local] = new
        if old is not None:
            if sent[old]["kind"] in CLOSABLE_KINDS:
                sent[old]["withdrawn"] = True
                withdrawn.append(old)
            else:
                findings.append(f"normative item {old} reworded as {new}")

    listed = {str(i.get("id", "")) for i in items}
    for uid, entry in sent.items():
        if entry.get("withdrawn") or uid in withdrawn or entry.get("local_id") in listed:
            continue
        if entry["kind"] in CLOSABLE_KINDS:
            entry["withdrawn"] = True
            withdrawn.append(uid)
        else:
            findings.append(f"normative item {uid} no longer listed by carcara")

    probes: list[dict[str, str]] = []
    for item in items:
        local = str(item.get("id", ""))
        res = results.get(local)
        if not isinstance(res, dict):
            continue
        probe = res.get("probe") or {}
        name, expect, result = probe.get("name"), probe.get("expect"), res.get("result")
        uid = current.get(local)
        if res.get("outcome") == "confirmed":
            if uid and sent[uid]["kind"] in CLOSABLE_KINDS and not sent[uid].get("probed"):
                note = f"probe {name}: {result} (expect {expect})"[:MAX_NOTE]
                probes.append({"item": uid, "note": note})
                sent[uid]["probed"] = True
        else:
            findings.append(
                f"probe {name} on {uid or local}: {res.get('outcome')}, {result} (expect {expect})"
            )

    probed = {p["item"] for p in probes}
    for uid in probed & set(withdrawn):
        withdrawn.remove(uid)
        sent[uid]["withdrawn"] = False
    if capped:
        findings.append(
            f"{capped} unverified item(s) not sent: Urutau keeps at most {MAX_ITEMS} per run"
        )
    return unverified, withdrawn, probes, findings, sent
