"""Memory backend abstractions for pluggable memory systems."""

from __future__ import annotations

from pathlib import Path

from nanobot.agent.memory import MemoryStore


class MemoryBackend:
    """Abstract memory backend interface."""

    def read_long_term(self) -> str:
        raise NotImplementedError

    def write_long_term(self, content: str) -> None:
        raise NotImplementedError

    def append_history(self, entry: str) -> None:
        raise NotImplementedError

    def get_memory_context(self) -> str:
        raise NotImplementedError

    def add_turn(self, user_input: str, assistant_response: str) -> None:
        """Ingest a single user/assistant turn into backend memory."""
        return None

    def retrieve_context(self, query: str) -> str:
        """Retrieve memory snippets relevant to the given query."""
        return ""


class LegacyMemoryBackend(MemoryBackend):
    """Default nanobot memory backend backed by MEMORY.md/HISTORY.md."""

    def __init__(self, workspace: Path):
        self.store = MemoryStore(workspace)

    def read_long_term(self) -> str:
        return self.store.read_long_term()

    def write_long_term(self, content: str) -> None:
        self.store.write_long_term(content)

    def append_history(self, entry: str) -> None:
        self.store.append_history(entry)

    def get_memory_context(self) -> str:
        return self.store.get_memory_context()
