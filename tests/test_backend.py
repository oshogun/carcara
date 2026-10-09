import asyncio
import os

import claude_agent_sdk
import pytest
from claude_agent_sdk import CLINotFoundError, ResultError, ResultMessage

from carcara.backend import (
    URUTAU_ENV_VARS,
    BackendError,
    BackendUnavailable,
    FakeBackend,
    SdkBackend,
    StageResult,
    build_request,
)
from carcara.policy import KNOWN_TOOLS, STRUCTURED_OUTPUT_TOOL
from carcara.profiles import load_profile
from carcara.roles import load_roles
from carcara.schemas import schema_for

ROLES = load_roles()
PROFILE = load_profile("quality")
CWD = "/repo"

IMPLEMENT_OK = {
    "changed": [{"path": "a.py", "summary": "x"}],
    "verified": "pytest",
    "notes": "",
    "blocked": False,
    "user_facing_change": False,
}
REVIEW_OK = {"verdict": "approve", "findings": [], "unverified": []}


def result_message(structured=None, subtype="success", is_error=False, cost=0.12, errors=None):
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=3,
        session_id="sess-1",
        total_cost_usd=cost,
        usage={"input_tokens": 10},
        result="done",
        structured_output=structured,
        errors=errors,
    )


def install_query(monkeypatch, messages, raise_after=None):
    calls = []

    async def fake_query(*, prompt, options=None, transport=None):
        calls.append({"prompt": prompt, "options": options})
        for message in messages:
            yield message
        if raise_after is not None:
            raise raise_after

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return calls


def run(backend, request):
    return asyncio.run(backend.run_stage(request))


def test_conftest_blocks_real_sdk():
    with pytest.raises(RuntimeError, match="real API call"):
        claude_agent_sdk.query(prompt="x")
    with pytest.raises(RuntimeError, match="real API call"):
        claude_agent_sdk.ClaudeSDKClient()


def test_implementer_option_mapping(monkeypatch):
    calls = install_query(monkeypatch, [result_message(IMPLEMENT_OK)])
    req = build_request(
        "implement",
        ROLES["implementer"],
        PROFILE,
        "do it",
        CWD,
        max_turns=30,
        max_budget_usd=2.5,
    )
    result = run(SdkBackend(), req)

    assert result.structured == IMPLEMENT_OK
    assert result.cost_usd == pytest.approx(0.12)
    assert result.session_id == "sess-1" and result.num_turns == 3
    assert not result.is_error

    opts = calls[0]["options"]
    assert calls[0]["prompt"] == "do it"
    assert opts.model == "opus"
    assert opts.permission_mode == "acceptEdits"
    assert "Edit" in opts.allowed_tools and "Edit" not in opts.disallowed_tools
    assert "Task" in opts.disallowed_tools and "Agent" in opts.disallowed_tools
    assert opts.cwd == CWD
    assert opts.max_turns == 30 and opts.max_budget_usd == 2.5
    assert opts.setting_sources == []
    assert opts.output_format == {
        "type": "json_schema",
        "schema": schema_for("implement"),
    }
    assert opts.system_prompt["preset"] == "claude_code"
    assert ROLES["implementer"].prompt in opts.system_prompt["append"]
    assert opts.can_use_tool is not None
    assert "PreToolUse" in opts.hooks and opts.hooks["PreToolUse"][0].hooks
    assert req.env == {"CARCARA_STAGE": "implementer"}
    assert opts.env == {"CARCARA_STAGE": "implementer"}


def test_reviewer_option_mapping_and_hook_denies_edit(monkeypatch):
    calls = install_query(monkeypatch, [result_message(REVIEW_OK)])
    req = build_request(
        "review", ROLES["reviewer"], PROFILE, "review", CWD, setting_sources=["project"]
    )
    run(SdkBackend(), req)
    opts = calls[0]["options"]
    assert opts.permission_mode == "dontAsk"
    assert "Edit" in opts.disallowed_tools and "Edit" not in opts.allowed_tools
    assert opts.can_use_tool is None
    assert opts.setting_sources == ["project"]
    assert opts.output_format["schema"] == schema_for("review")

    hook = opts.hooks["PreToolUse"][0].hooks[0]
    out = asyncio.run(hook({"tool_name": "Edit", "tool_input": {"file_path": "a.py"}}, None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "a.py"}}, None, None))
    assert ok == {}


def test_main_model_request_has_no_tools(monkeypatch):
    calls = install_query(
        monkeypatch,
        [
            result_message(
                {"size": "S", "rationale": "tiny", "triageRange": "S", "uncertaintyKind": "none"}
            )
        ],
    )
    req = build_request("triage", None, PROFILE, "triage this", CWD, max_turns=2)
    assert req.role is None and req.model == "opus"
    assert req.allowed_tools == [STRUCTURED_OUTPUT_TOOL]
    assert req.disallowed_tools == list(KNOWN_TOOLS)
    assert "*" not in req.disallowed_tools
    result = run(SdkBackend(), req)
    assert result.structured["size"] == "S"
    opts = calls[0]["options"]
    assert opts.permission_mode == "dontAsk" and opts.can_use_tool is None
    assert opts.env == {"CARCARA_STAGE": "main"}
    hook = opts.hooks["PreToolUse"][0].hooks[0]
    out = asyncio.run(hook({"tool_name": "Read", "tool_input": {}}, None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_on_message_callback(monkeypatch):
    msg = result_message(REVIEW_OK)
    install_query(monkeypatch, ["progress", msg])
    seen = []
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    run(SdkBackend(on_message=seen.append), req)
    assert seen == ["progress", msg]


def test_cli_not_found_maps_to_unavailable(monkeypatch):
    install_query(monkeypatch, [], raise_after=CLINotFoundError())
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendUnavailable, match="Claude Code CLI not found"):
        run(SdkBackend(), req)


def test_other_sdk_error_maps_to_backend_error(monkeypatch):
    install_query(monkeypatch, [], raise_after=claude_agent_sdk.ProcessError("boom", exit_code=2))
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendError, match="boom") as info:
        run(SdkBackend(), req)
    assert not isinstance(info.value, BackendUnavailable)


def test_error_subtype_returns_error_result(monkeypatch):
    msg = result_message(None, subtype="error_max_turns", is_error=True, errors=["too many"])
    err = ResultError("x", data={"subtype": "error_max_turns", "is_error": True})
    install_query(monkeypatch, [msg], raise_after=err)
    req = build_request("implement", ROLES["implementer"], PROFILE, "p", CWD)
    result = run(SdkBackend(), req)
    assert result.is_error and result.subtype == "error_max_turns"
    assert result.errors == ["too many"] and result.structured is None


def test_invalid_structured_output_raises(monkeypatch):
    install_query(monkeypatch, [result_message({"verdict": "maybe", "findings": []})])
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendError, match="invalid output"):
        run(SdkBackend(), req)


def test_missing_structured_output_raises(monkeypatch):
    install_query(monkeypatch, [result_message(None)])
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendError, match="no structured output"):
        run(SdkBackend(), req)


def test_no_result_message_raises(monkeypatch):
    install_query(monkeypatch, [])
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendError, match="no result message"):
        run(SdkBackend(), req)


# --- FakeBackend -------------------------------------------------------------


def test_fake_backend_consumes_script_in_order_and_records():
    custom = StageResult(structured=None, subtype="error_max_turns", is_error=True)
    fake = FakeBackend(
        {"review": [REVIEW_OK, custom], "implement": [IMPLEMENT_OK]},
        costs={"review": 0.5},
    )
    review = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    first = run(fake, review)
    assert first.structured == REVIEW_OK and first.cost_usd == 0.5
    assert run(fake, review) is custom
    impl = build_request("implement", ROLES["implementer"], PROFILE, "p", CWD)
    assert run(fake, impl).cost_usd == 0.0
    assert [r.stage for r in fake.requests] == ["review", "review", "implement"]
    with pytest.raises(BackendError, match="exhausted"):
        run(fake, review)


def test_fake_backend_prefers_key_over_stage_queue():
    keyed = dict(REVIEW_OK, verdict="request_changes")
    fake = FakeBackend(
        {"review-dim:security": [keyed], "review-dim": [REVIEW_OK]},
        costs={"review-dim": 0.1, "review-dim:security": 0.3},
    )
    sec = build_request(
        "review-dim", ROLES["reviewer"], PROFILE, "p", CWD, key="review-dim:security"
    )
    assert sec.key == "review-dim:security"
    first = run(fake, sec)
    assert first.structured == keyed and first.cost_usd == 0.3
    other = build_request(
        "review-dim", ROLES["reviewer"], PROFILE, "p", CWD, key="review-dim:tests"
    )
    second = run(fake, other)
    assert second.structured == REVIEW_OK and second.cost_usd == 0.1


def test_fake_backend_raises_exception_entries():
    fake = FakeBackend({"review": [BackendError("boom")]})
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendError, match="boom"):
        run(fake, req)


def test_fake_backend_validates_entries():
    fake = FakeBackend({"review": [{"verdict": "nope"}]})
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendError, match="invalid output"):
        run(fake, req)


def test_structured_output_tool_allowed_for_every_stage(monkeypatch):
    calls = install_query(monkeypatch, [result_message(REVIEW_OK), result_message(REVIEW_OK)])
    for role in (None, ROLES["reviewer"]):
        run(SdkBackend(), build_request("review", role, PROFILE, "p", CWD))
    for call in calls:
        opts = call["options"]
        assert STRUCTURED_OUTPUT_TOOL in opts.allowed_tools
        assert STRUCTURED_OUTPUT_TOOL not in opts.disallowed_tools
        hook = opts.hooks["PreToolUse"][0].hooks[0]
        out = asyncio.run(hook({"tool_name": STRUCTURED_OUTPUT_TOOL, "tool_input": {}}, None, None))
        assert out == {}
    main_hook = calls[0]["options"].hooks["PreToolUse"][0].hooks[0]
    denied = asyncio.run(main_hook({"tool_name": "Read", "tool_input": {}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_error_subtype_without_is_error_flag_is_error_result(monkeypatch):
    msg = result_message(None, subtype="error_max_turns", is_error=False, cost=0.008)
    install_query(monkeypatch, [msg])
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    result = run(SdkBackend(), req)
    assert result.is_error and result.cost_usd == pytest.approx(0.008)


def test_invalid_output_error_carries_result_cost(monkeypatch):
    install_query(monkeypatch, [result_message(None, cost=0.008)])
    req = build_request("review", ROLES["reviewer"], PROFILE, "p", CWD)
    with pytest.raises(BackendError, match="no structured output") as info:
        run(SdkBackend(), req)
    assert info.value.result is not None
    assert info.value.result.cost_usd == pytest.approx(0.008)


KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def install_env_capturing_query(monkeypatch, raise_after=None):
    seen = []

    async def fake_query(*, prompt, options=None, transport=None):
        # The real transport snapshots os.environ when the generator starts.
        seen.append({k: os.environ.get(k) for k in KEY_VARS})
        yield result_message(IMPLEMENT_OK)
        if raise_after is not None:
            raise raise_after

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return seen


def impl_request():
    return build_request("implement", ROLES["implementer"], PROFILE, "do it", CWD)


def test_sdk_backend_hides_api_keys_by_default_and_restores(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test")
    seen = install_env_capturing_query(monkeypatch)
    run(SdkBackend(), impl_request())
    assert seen == [{"ANTHROPIC_API_KEY": None, "ANTHROPIC_AUTH_TOKEN": None}]
    assert not set(KEY_VARS) & set(impl_request().env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "tok-test"


def test_sdk_backend_restores_api_keys_on_exception(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    seen = install_env_capturing_query(
        monkeypatch, raise_after=claude_agent_sdk.ProcessError("boom", exit_code=2)
    )
    with pytest.raises(BackendError):
        run(SdkBackend(), impl_request())
    assert seen[0]["ANTHROPIC_API_KEY"] is None
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_sdk_backend_use_api_key_passes_keys_through(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test")
    seen = install_env_capturing_query(monkeypatch)
    run(SdkBackend(use_api_key=True), impl_request())
    assert seen == [{"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "tok-test"}]


@pytest.mark.parametrize("use_api_key", [False, True])
def test_sdk_backend_hides_urutau_env_and_restores(monkeypatch, use_api_key):
    monkeypatch.setenv("URUTAU_MCP_TOKEN", "tok-SECRET-123")
    monkeypatch.setenv("URUTAU_URL", "http://127.0.0.1:8787")
    seen = []

    async def fake_query(*, prompt, options=None, transport=None):
        seen.append({k: os.environ.get(k) for k in URUTAU_ENV_VARS})
        yield result_message(IMPLEMENT_OK)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    req = impl_request()
    run(SdkBackend(use_api_key=use_api_key), req)
    assert seen == [{"URUTAU_MCP_TOKEN": None, "URUTAU_URL": None}]
    assert not set(URUTAU_ENV_VARS) & set(req.env)
    assert os.environ["URUTAU_MCP_TOKEN"] == "tok-SECRET-123"
    assert os.environ["URUTAU_URL"] == "http://127.0.0.1:8787"


@pytest.mark.parametrize("flag", [False, True])
def test_unrestricted_bash_reaches_policy_callbacks(monkeypatch, flag):
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    calls = install_query(monkeypatch, [result_message(IMPLEMENT_OK)])
    kwargs = {"unrestricted_bash": True} if flag else {}
    req = build_request("implement", ROLES["implementer"], PROFILE, "do it", CWD, **kwargs)
    assert req.unrestricted_bash is flag
    run(SdkBackend(), req)
    opts = calls[0]["options"]
    push = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
    out = asyncio.run(opts.hooks["PreToolUse"][0].hooks[0](push, None, None))
    assert (out == {}) is flag
    res = asyncio.run(opts.can_use_tool("Bash", push["tool_input"], None))
    assert isinstance(res, PermissionResultAllow if flag else PermissionResultDeny)
