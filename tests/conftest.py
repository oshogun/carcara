import pytest


def _no_real_api(*args, **kwargs):
    raise RuntimeError("real API call in tests")


@pytest.fixture(autouse=True)
def _block_real_sdk(monkeypatch):
    """Fail loudly if any test reaches the real Claude Agent SDK."""
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", _no_real_api)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _no_real_api)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate tests from the caller's Claude Code / carcara / billing env.

    Subprocess-based tests inherit ``os.environ``, so this covers them too.
    """
    for name in (
        "CLAUDECODE",
        "CARCARA_STAGE",
        "CARCARA_OFF",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
