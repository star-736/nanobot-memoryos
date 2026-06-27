from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_backend import MemoryOSBackend
from nanobot.config.schema import Config


class _RetrievalMemory(MemoryStore):
    def __init__(self, workspace):
        super().__init__(workspace)
        self.calls: list[dict[str, Any]] = []

    def retrieve_context(self, query: str, session_key: str | None = None, recent_history=None) -> str:
        self.calls.append({
            "query": query,
            "session_key": session_key,
            "recent_history": recent_history,
        })
        return "remembered preference"


def test_memoryos_config_accepts_camel_case_keys():
    cfg = Config.model_validate({
        "memory": {
            "backend": "memoryos",
            "memoryos": {
                "memoryScope": "global",
                "memoryUserId": "owner",
                "dataStoragePath": "~/.nanobot/workspace/memoryos_data",
                "embeddingModelName": "all-MiniLM-L6-v2",
                "openaiApiKey": "memory-key",
                "openaiBaseUrl": "https://example.test/v1",
            },
        },
    })

    assert cfg.memory.backend == "memoryos"
    assert cfg.memory.memoryos.memory_scope == "global"
    assert cfg.memory.memoryos.memory_user_id == "owner"
    assert cfg.memory.memoryos.openai_api_key == "memory-key"
    assert cfg.memory.memoryos.openai_base_url == "https://example.test/v1"


def test_context_builder_injects_retrieved_memory(tmp_path):
    memory = _RetrievalMemory(tmp_path)
    builder = ContextBuilder(tmp_path, memory_backend=memory)

    messages = builder.build_messages(
        history=[{"role": "user", "content": "old question"}],
        current_message="what do I prefer?",
        session_key="telegram:chat-1",
    )

    assert memory.calls == [{
        "query": "what do I prefer?",
        "session_key": "telegram:chat-1",
        "recent_history": [{"role": "user", "content": "old question"}],
    }]
    assert "# Retrieved Memory" in messages[0]["content"]
    assert "remembered preference" in messages[0]["content"]


def test_context_builder_can_skip_retrieved_memory(tmp_path):
    memory = _RetrievalMemory(tmp_path)
    builder = ContextBuilder(tmp_path, memory_backend=memory)

    messages = builder.build_messages(
        history=[],
        current_message="[token-probe]",
        session_key="telegram:chat-1",
        include_retrieved_memory=False,
    )

    assert memory.calls == []
    assert "# Retrieved Memory" not in messages[0]["content"]


def test_memoryos_expands_configured_storage_path(tmp_path, monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeMemoryos:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType("nanobot.memoryos_core.memoryos")
    fake_module.Memoryos = _FakeMemoryos
    monkeypatch.setitem(sys.modules, "nanobot.memoryos_core.memoryos", fake_module)
    backend = MemoryOSBackend(
        tmp_path,
        default_model="model",
        api_key="key",
        memoryos_config={"data_storage_path": "~/nanobot-memoryos-test"},
    )

    backend._get_instance("session")

    assert "~" not in captured["data_storage_path"]


def test_ephemeral_outbound_does_not_persist_memory(tmp_path):
    provider = MagicMock()
    provider.get_default_model.return_value = "model"
    provider.generation.max_tokens = 100
    loop = AgentLoop(provider=provider, bus=MagicMock(), workspace=tmp_path)
    loop.memory_backend.add_turn = MagicMock()
    loop.tools.get = MagicMock(return_value=None)

    msg = MagicMock()
    msg.channel = "cli"
    msg.sender_id = "user"
    msg.chat_id = "direct"
    msg.content = "internal prompt"
    msg.metadata = {}

    outbound = loop._assemble_outbound(
        msg,
        "internal result",
        [],
        "",
        False,
        None,
        session_key="sdk:ephemeral",
        persist_memory=False,
    )

    assert outbound is not None
    loop.memory_backend.add_turn.assert_not_called()
