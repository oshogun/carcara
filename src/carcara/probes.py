"""Allow-listed, read-only probes for reviewer ``external`` unverified items.

A probe is a named URL template from ``.carcara/config.json`` (``probes``).
The orchestrator, not an agent, sends one unauthenticated HTTP GET per
referenced probe and records only the status. The agent controls only the
``{arg}`` value, which is URL-quoted into the configured template. No body is
read, and no credentials or cookies are sent. Redirects are not followed
(the outcome is inconclusive) and proxy environment variables are ignored.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from carcara import __version__

MAX_PROBES_PER_CALL = 10
MAX_RESULT_CHARS = 80
USER_AGENT = f"carcara/{__version__} (probe)"

_OPPOSITE = {"exists": "absent", "absent": "exists"}


class _Redirected(urllib.error.URLError):
    """A 3xx response; probes never follow redirects."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise _Redirected(f"redirect {code}")


def _default_opener() -> urllib.request.OpenerDirector:
    """urlopen-like opener that ignores proxy env vars and refuses redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _status_of(resp: Any) -> int | None:
    status = getattr(resp, "status", None)
    if status is None and hasattr(resp, "getcode"):
        status = resp.getcode()
    return status if isinstance(status, int) else None


def _fetch(url: str, timeout: float, opener: Callable[..., Any]) -> tuple[int | None, str]:
    """GET ``url``; return (status or None, short result text)."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        resp = opener(req, timeout=timeout)
    except _Redirected:
        return None, "redirect"
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        if 300 <= e.code < 400:
            return None, "redirect"
        return e.code, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = e.reason
        text = "timeout" if isinstance(reason, TimeoutError) else str(reason)
        return None, f"error: {text}"
    except TimeoutError:
        return None, "error: timeout"
    except (OSError, ValueError) as e:
        return None, f"error: {e}"
    try:
        status = _status_of(resp)
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()
    if status is not None and 300 <= status < 400:
        return None, "redirect"
    return status, f"HTTP {status}" if status is not None else "error: no status"


def _observed(status: int | None) -> str | None:
    if status is None:
        return None
    if 200 <= status < 300:
        return "exists"
    if status in (404, 410):
        return "absent"
    return None


def run_probes(
    items: Iterable[Mapping[str, Any]],
    probes: Mapping[str, str],
    *,
    timeout: float = 5.0,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, str]]:
    """Run the allow-listed probes referenced by ``external`` items.

    Items that are not ``external``, have no ``probe``, or name a probe not in
    ``probes`` are skipped. At most ``MAX_PROBES_PER_CALL`` requests are sent.
    Returns ``[{id, probe, outcome, result}]`` where outcome is ``confirmed``,
    ``contradicted`` or ``inconclusive`` relative to the item's ``expect``.
    """
    if not probes:
        return []
    send = opener or _default_opener().open
    results: list[dict[str, str]] = []
    for item in items:
        if len(results) >= MAX_PROBES_PER_CALL:
            break
        if item.get("kind") != "external":
            continue
        probe = item.get("probe")
        if not isinstance(probe, Mapping):
            continue
        name = probe.get("name")
        template = probes.get(name) if isinstance(name, str) else None
        if not template:
            continue
        arg = str(probe.get("arg", ""))
        url = template.replace("{arg}", urllib.parse.quote(arg, safe=""))
        status, result = _fetch(url, timeout, send)
        observed = _observed(status)
        expect = probe.get("expect")
        if observed is not None and observed == expect:
            outcome = "confirmed"
        elif observed is not None and _OPPOSITE.get(expect) == observed:
            outcome = "contradicted"
        else:
            outcome = "inconclusive"
        results.append({
            "id": str(item.get("id", "")),
            "probe": name,
            "outcome": outcome,
            "result": result[:MAX_RESULT_CHARS],
        })
    return results
