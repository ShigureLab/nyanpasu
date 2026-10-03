from __future__ import annotations

from nyanpasu.backends import Backends
from nyanpasu.claude import ClaudeBackend
from nyanpasu.codex import CodexAppServerBackend
from nyanpasu.config import NyanpasuConfig


def test_named_registry_is_lazy_and_caches_by_backend_name(tmp_path):
    config = NyanpasuConfig.model_validate(
        {
            "state_dir": tmp_path,
            "backends": {
                "codex": {"driver": "codex"},
                "fast": {"driver": "codex", "defaults": {"model": "fast-model"}},
                "review": {"driver": "claude-code"},
            },
        }
    )
    backends = Backends(config)
    assert backends._instances == {}
    assert set(backends.runtime_info()["backends"]) == {"codex", "fast", "review"}
    assert backends._instances == {}  # Dashboard reads do not launch or resolve credentials.
    first = backends.get("codex")
    fast = backends.get("fast")
    assert first is backends.get("codex") and first is not fast
    assert isinstance(fast.execution, CodexAppServerBackend)
    assert fast.execution.config.defaults.model == "fast-model"
    assert isinstance(backends.get("review").execution, ClaudeBackend)
