import io
import types
import urllib.error
import urllib.request

import pytest

from carcara import probes
from carcara.probes import MAX_PROBES_PER_CALL, run_probes

PROBES = {"pypi-name": "https://pypi.org/pypi/{arg}/json"}


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def read(self, *a):  # pragma: no cover - must never be called
        raise AssertionError("probe body must not be read")

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, status=200, exc=None):
        self.status, self.exc = status, exc
        self.requests, self.timeouts = [], []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        if self.exc is not None:
            raise self.exc
        return FakeResp(self.status)


def item(id="U1", kind="external", name="pypi-name", arg="carcara-sdlc", expect="exists"):
    it = {"id": id, "kind": kind, "text": "package name is free"}
    if name is not None:
        it["probe"] = {"name": name, "arg": arg, "expect": expect}
    return it


def test_get_with_quoted_arg_and_no_auth():
    op = FakeOpener(200)
    out = run_probes([item(arg="a b/c")], PROBES, opener=op)
    (req,) = op.requests
    assert req.get_method() == "GET"
    assert req.full_url == "https://pypi.org/pypi/a%20b%2Fc/json"
    assert req.get_header("Authorization") is None
    assert req.get_header("Cookie") is None
    assert set(req.header_items()) == {("User-agent", req.get_header("User-agent"))}
    assert op.timeouts == [5.0]
    assert out == [{"id": "U1", "probe": "pypi-name", "outcome": "confirmed", "result": "HTTP 200"}]


def test_skips_unconfigured_and_non_external():
    op = FakeOpener(200)
    out = run_probes(
        [item(name="other"), item(id="U2", kind="normative"), item(id="U3", name=None)],
        PROBES,
        opener=op,
    )
    assert out == [] and op.requests == []


def test_empty_probes_never_calls_opener():
    op = FakeOpener(200)
    assert run_probes([item()], {}, opener=op) == []
    assert op.requests == []


def _http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO())


def test_404_expect_absent_confirmed():
    out = run_probes([item(expect="absent")], PROBES, opener=FakeOpener(exc=_http_error(404)))
    assert out[0]["outcome"] == "confirmed" and out[0]["result"] == "HTTP 404"


def test_200_expect_absent_contradicted():
    out = run_probes([item(expect="absent")], PROBES, opener=FakeOpener(200))
    assert out[0]["outcome"] == "contradicted"


def test_other_status_inconclusive():
    out = run_probes([item()], PROBES, opener=FakeOpener(exc=_http_error(503)))
    assert out[0]["outcome"] == "inconclusive" and out[0]["result"] == "HTTP 503"


def test_url_error_inconclusive():
    out = run_probes([item()], PROBES, opener=FakeOpener(exc=urllib.error.URLError("x" * 200)))
    assert out[0]["outcome"] == "inconclusive"
    assert out[0]["result"].startswith("error: ")
    assert len(out[0]["result"]) <= 80


def test_timeout_result():
    out = run_probes([item()], PROBES, opener=FakeOpener(exc=TimeoutError()))
    assert out[0] == {
        "id": "U1",
        "probe": "pypi-name",
        "outcome": "inconclusive",
        "result": "error: timeout",
    }


def test_capped_per_call():
    op = FakeOpener(200)
    out = run_probes([item(id=f"U{i}") for i in range(15)], PROBES, opener=op)
    assert len(out) == len(op.requests) == MAX_PROBES_PER_CALL


def test_redirect_status_inconclusive():
    for code in (301, 302, 307):
        out = run_probes([item(expect="absent")], PROBES, opener=FakeOpener(exc=_http_error(code)))
        assert (out[0]["outcome"], out[0]["result"]) == ("inconclusive", "redirect")
    out = run_probes([item()], PROBES, opener=FakeOpener(304))
    assert (out[0]["outcome"], out[0]["result"]) == ("inconclusive", "redirect")


def test_no_redirect_handler_refuses():
    req = urllib.request.Request("https://pypi.org/pypi/x/json")
    with pytest.raises(probes._Redirected):
        probes._NoRedirect().redirect_request(req, None, 302, "Found", {}, "http://evil/")
    out = run_probes([item()], PROBES, opener=FakeOpener(exc=probes._Redirected("redirect 302")))
    assert (out[0]["outcome"], out[0]["result"]) == ("inconclusive", "redirect")


def test_default_opener_ignores_proxies_and_redirects(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    assert urllib.request.ProxyHandler().proxies  # the env var would be honoured by default
    opener = probes._default_opener()
    # ProxyHandler({}) has no proxy_open methods, so no proxy handler is installed.
    assert not [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    redirects = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert redirects and all(isinstance(h, probes._NoRedirect) for h in redirects)


def test_run_probes_uses_default_opener(monkeypatch):
    op = FakeOpener(404)
    monkeypatch.setattr(probes, "_default_opener", lambda: types.SimpleNamespace(open=op))
    out = run_probes([item(expect="absent")], PROBES)
    assert len(op.requests) == 1 and out[0]["outcome"] == "confirmed"
