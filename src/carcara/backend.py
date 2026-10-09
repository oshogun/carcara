"""Stage execution backends for ``carcara run``.

``SdkBackend`` runs one pipeline stage as a fresh ``claude_agent_sdk.query``
session; ``FakeBackend`` replays scripted outputs for tests. Both take a
``StageRequest`` (assembled by ``build_request`` from roles + policy) and
return a ``StageResult`` whose ``structured`` output was validated against the
stage's JSON schema.

SDK notes (claude-agent-sdk 0.2.x):

- ``can_use_tool`` works with a plain string prompt (the SDK always uses
  streaming mode internally). It is only passed for non-``dontAsk`` modes,
  since ``dontAsk`` denies instead of consulting the callback. Whole-tool
  ``allowed_tools`` entries shadow it (``CanUseToolShadowedWarning``, which is
  silenced); the PreToolUse hook remains the authoritative gate.
- When the CLI reports an error result it yields the ``ResultMessage`` and
  then raises ``ResultError``; that case is returned as an ``is_error`` result
  rather than raised.
- Billing: the SDK spawns the ``claude`` CLI with ``{**os.environ, **options.env}``
  and has no way to *remove* an inherited variable (an empty string would still
  count as set). Unless ``use_api_key`` is true, ``SdkBackend`` therefore drops
  ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` from ``os.environ`` while a
  stage runs and restores them afterwards, so the CLI uses the user's Claude
  subscription login. This is process-global, which is fine for the
  single-run, sequential ``carcara run`` CLI. ``StageRequest.env`` (which
  sets ``CARCARA_STAGE``) is passed as ``options.env`` and never contains
  those keys. The Urutau settings (``URUTAU_ENV_VARS``) are always dropped
  the same way, so agent stages never see the Urutau token.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from carcara import policy
from carcara.roles import Role, model_for
from carcara.schemas import schema_for, validate

if TYPE_CHECKING:
    from carcara.profiles import Profile

STAGE_SCHEMA_NOTE = (
    "\n\n## carcara run output\n"
    "You are running headless inside `carcara run`; no human will answer questions. "
    "Your final answer is captured as structured JSON matching the stage's output "
    "schema. That schema replaces any reply format described above; put the same "
    "information into its fields.\n"
)

MAIN_PROMPT = (
    "You are the main orchestrating model of the carcara SDLC pipeline. "
    "You have no tools for this stage: answer from the information in the prompt."
)


API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Urutau MCP settings: only carcara itself talks to Urutau; stages never see them.
URUTAU_ENV_VARS = ("URUTAU_MCP_TOKEN", "URUTAU_URL")
# Set to the stage's role name (or "main") in every stage's CLI process, so
# carcara's Claude Code hooks no-op there and `carcara run` refuses to nest.
STAGE_ENV_VAR = "CARCARA_STAGE"


@contextmanager
def _without_env(names: tuple[str, ...]) -> Iterator[None]:
    """Temporarily remove ``names`` from ``os.environ`` (process-global)."""
    saved = {name: os.environ.pop(name) for name in names if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


class BackendError(Exception):
    """A stage failed or produced invalid output.

    ``result`` carries the stage's ``StageResult`` when one was received, so
    the caller can still account for its cost and usage.
    """

    def __init__(self, message: str, result: StageResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class NoStructuredOutput(BackendError):
    """The stage finished without emitting its structured output."""


class BackendUnavailable(BackendError):
    """The backend cannot run at all (e.g. Claude Code CLI missing)."""


@dataclass
class StageRequest:
    stage: str
    role: str | None
    model: str
    system_prompt: dict[str, Any]
    prompt: str
    allowed_tools: list[str]
    disallowed_tools: list[str]
    permission_mode: str
    cwd: str
    output_schema: dict[str, Any]
    max_turns: int | None = None
    max_budget_usd: float | None = None
    setting_sources: list[str] = field(default_factory=list)
    # Extra environment for the stage's CLI process (merged over os.environ).
    env: dict[str, str] = field(default_factory=dict)
    # From ``carcara run --unrestricted-bash``: skip the implementer/test-runner deny-list.
    unrestricted_bash: bool = False
    # The orchestrator's stage key (e.g. ``review-dim:security``); lets
    # FakeBackend script parallel siblings of one stage individually.
    key: str | None = None


@dataclass
class StageResult:
    structured: Any = None
    text: str | None = None
    cost_usd: float = 0.0
    usage: dict[str, Any] | None = None
    num_turns: int = 0
    session_id: str | None = None
    subtype: str = "success"
    is_error: bool = False
    errors: list[str] = field(default_factory=list)


def build_request(
    stage: str,
    role: Role | None,
    profile: Profile | Mapping[str, str],
    prompt: str,
    cwd: str,
    *,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    setting_sources: list[str] | None = None,
    unrestricted_bash: bool = False,
    key: str | None = None,
    schema: str | None = None,
) -> StageRequest:
    """Assemble a stage request; ``role=None`` means the main model with no tools.

    ``schema`` names the output schema when it differs from ``stage``.
    """
    if role is None:
        model = model_for("main", profile)
        append = MAIN_PROMPT + STAGE_SCHEMA_NOTE
        # Only StructuredOutput (how the CLI returns json_schema output). Never
        # use "*" in disallowed_tools: it would remove StructuredOutput too.
        allowed: list[str] = [policy.STRUCTURED_OUTPUT_TOOL]
        disallowed = [t for t in policy.KNOWN_TOOLS if t != policy.STRUCTURED_OUTPUT_TOOL]
        mode = "dontAsk"
        role_name = None
    else:
        model = model_for(role, profile)
        append = role.prompt + STAGE_SCHEMA_NOTE
        allowed = policy.allowed_tools_for(role)
        disallowed = policy.disallowed_tools_for(role)
        mode = policy.permission_mode_for(role)
        role_name = role.name
    return StageRequest(
        stage=stage,
        role=role_name,
        model=model,
        system_prompt={"type": "preset", "preset": "claude_code", "append": append},
        prompt=prompt,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        permission_mode=mode,
        cwd=cwd,
        output_schema=schema_for(schema or stage),
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        setting_sources=list(setting_sources or []),
        env={STAGE_ENV_VAR: role_name or "main"},
        unrestricted_bash=unrestricted_bash,
        key=key,
    )


class Backend(Protocol):
    async def run_stage(self, request: StageRequest) -> StageResult: ...


async def _deny_all_hook(
    input_data: dict[str, Any], tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    if input_data.get("tool_name") == policy.STRUCTURED_OUTPUT_TOOL:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "carcara policy: no tools in this stage",
        }
    }


def _check_structured(
    request: StageRequest, structured: Any, result: StageResult | None = None
) -> None:
    if structured is None:
        raise NoStructuredOutput(f"stage {request.stage}: no structured output", result)
    errors = validate(request.output_schema, structured)
    if errors:
        raise BackendError(f"stage {request.stage}: invalid output: " + "; ".join(errors), result)


class SdkBackend:
    """Runs each stage via ``claude_agent_sdk.query`` (imported lazily)."""

    def __init__(
        self,
        on_message: Callable[[Any], None] | None = None,
        *,
        use_api_key: bool = False,
    ) -> None:
        self.on_message = on_message
        # False: hide API_KEY_ENV_VARS from the CLI so it uses the subscription login.
        self.use_api_key = use_api_key

    def build_options(self, request: StageRequest) -> Any:
        import claude_agent_sdk as sdk

        if request.role is None:
            hook = _deny_all_hook
            can_use_tool = None
        else:
            unrestricted = request.unrestricted_bash
            hook = policy.make_pre_tool_use_hook(
                request.role, request.cwd, unrestricted_bash=unrestricted
            )
            can_use_tool = (
                None
                if request.permission_mode == "dontAsk"
                else policy.make_can_use_tool(
                    request.role, request.cwd, unrestricted_bash=unrestricted
                )
            )
        return sdk.ClaudeAgentOptions(
            model=request.model,
            allowed_tools=list(request.allowed_tools),
            disallowed_tools=list(request.disallowed_tools),
            permission_mode=request.permission_mode,
            cwd=request.cwd,
            system_prompt=request.system_prompt,
            output_format={"type": "json_schema", "schema": request.output_schema},
            max_turns=request.max_turns,
            max_budget_usd=request.max_budget_usd,
            setting_sources=list(request.setting_sources),
            hooks={"PreToolUse": [sdk.HookMatcher(matcher=None, hooks=[hook])]},
            can_use_tool=can_use_tool,
            env=dict(request.env),
        )

    async def run_stage(self, request: StageRequest) -> StageResult:
        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise BackendUnavailable(f"claude-agent-sdk is not installed: {exc}") from exc

        options = self.build_options(request)
        result_msg: Any = None
        hidden = URUTAU_ENV_VARS + (() if self.use_api_key else API_KEY_ENV_VARS)
        try:
            with _without_env(hidden), warnings.catch_warnings():
                warnings.simplefilter("ignore", sdk.CanUseToolShadowedWarning)
                async for message in sdk.query(prompt=request.prompt, options=options):
                    if self.on_message is not None:
                        self.on_message(message)
                    if isinstance(message, sdk.ResultMessage):
                        result_msg = message
        except sdk.CLINotFoundError as exc:
            raise BackendUnavailable(
                "Claude Code CLI not found; install it "
                "(https://code.claude.com/docs/en/setup) and make sure `claude` is on PATH"
            ) from exc
        except sdk.ClaudeSDKError as exc:
            if result_msg is None or not result_msg.is_error:
                partial = None if result_msg is None else _to_result(result_msg)
                raise BackendError(f"stage {request.stage}: {exc}", partial) from exc
        if result_msg is None:
            raise BackendError(f"stage {request.stage}: no result message")

        result = _to_result(result_msg)
        if not result.is_error:
            _check_structured(request, result.structured, result)
        return result


def _to_result(result_msg: Any) -> StageResult:
    subtype = result_msg.subtype
    return StageResult(
        structured=result_msg.structured_output,
        text=result_msg.result,
        cost_usd=float(result_msg.total_cost_usd or 0.0),
        usage=result_msg.usage,
        num_turns=result_msg.num_turns,
        session_id=result_msg.session_id,
        subtype=subtype,
        is_error=bool(result_msg.is_error) or str(subtype or "").startswith("error"),
        errors=list(result_msg.errors or []),
    )


class FakeBackend:
    """Scripted backend: ``script[stage]`` is consumed in order.

    ``script[key]`` (the request's stage key, e.g. ``review-dim:security``)
    takes precedence over ``script[stage]`` when present. Entries are
    structured outputs (validated against the stage schema), ready-made
    ``StageResult`` objects, or exception instances (raised). ``costs[key]``
    or ``costs[stage]`` sets ``cost_usd`` for structured entries.
    """

    def __init__(
        self,
        script: Mapping[str, list[Any]],
        costs: Mapping[str, float] | None = None,
    ) -> None:
        self.script: dict[str, list[Any]] = {k: list(v) for k, v in script.items()}
        self.costs = dict(costs or {})
        self.requests: list[StageRequest] = []

    async def run_stage(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        key = request.key
        name = key if key is not None and key in self.script else request.stage
        queue = self.script.get(name)
        if not queue:
            raise BackendError(f"FakeBackend: script exhausted for stage {name}")
        entry = queue.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, StageResult):
            return entry
        _check_structured(request, entry)
        cost = self.costs.get(request.stage, 0.0)
        if key is not None:
            cost = self.costs.get(key, cost)
        return StageResult(
            structured=entry,
            text=None,
            cost_usd=cost,
            num_turns=1,
            session_id=f"fake-{request.stage}-{len(self.requests)}",
        )
