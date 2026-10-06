import pytest


def _no_real_api(*args, **kwargs):
    raise RuntimeError("real API call in tests")


@pytest.fixture(autouse=True)
def _block_real_sdk(monkeypatch):
    """Fail loudly if any test reaches the real Claude Agent SDK."""
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", _no_real_api)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _no_real_api)
