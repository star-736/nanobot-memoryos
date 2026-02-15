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

    def add_turn(self, user_input: str, assistant_response: str, session_key: str | None = None) -> None:
        """Ingest a single user/assistant turn into backend memory."""
        return None

    def retrieve_context(self, query: str, session_key: str | None = None) -> str:
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


class MemoryOSBackend(MemoryBackend):
    """MemoryOS-backed memory backend with safe fallback behavior."""

    def __init__(
        self,
        workspace: Path,
        *,
        default_model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        memoryos_config: dict | None = None,
    ):
        self.workspace = workspace
        self.legacy = LegacyMemoryBackend(workspace)
        self.default_model = default_model
        self.api_key = api_key or ""
        self.api_base = api_base or ""
        self.memoryos_config = memoryos_config or {}
        self._instances: dict[str, object] = {}
        self._enabled = True

    def _normalize_key(self, session_key: str | None) -> str:
        if not session_key:
            return "default"
        return session_key.replace(":", "_").replace("/", "_")

    def _get_instance(self, session_key: str | None):
        key = self._normalize_key(session_key)
        if key in self._instances:
            return self._instances[key]

        if not self.api_key:
            raise RuntimeError("MemoryOS requires an API key")

        try:
            from nanobot.memoryos_core.memoryos import Memoryos
        except Exception as exc:
            raise RuntimeError(f"MemoryOS import failed: {exc}") from exc

        cfg = self.memoryos_config
        data_storage_path = cfg.get("data_storage_path") or str(self.workspace / "memoryos_data")
        llm_model = cfg.get("llm_model") or self.default_model
        embedding_model_kwargs = cfg.get("embedding_model_kwargs")

        instance = Memoryos(
            user_id=key,
            assistant_id="nanobot",
            openai_api_key=self.api_key,
            openai_base_url=self.api_base or None,
            data_storage_path=data_storage_path,
            llm_model=llm_model,
            short_term_capacity=cfg.get("short_term_capacity", 10),
            mid_term_capacity=cfg.get("mid_term_capacity", 2000),
            long_term_knowledge_capacity=cfg.get("long_term_knowledge_capacity", 100),
            retrieval_queue_capacity=cfg.get("retrieval_queue_capacity", 7),
            mid_term_heat_threshold=cfg.get("mid_term_heat_threshold", 5.0),
            mid_term_similarity_threshold=cfg.get("mid_term_similarity_threshold", 0.6),
            embedding_model_name=cfg.get("embedding_model_name", "all-MiniLM-L6-v2"),
            embedding_model_kwargs=embedding_model_kwargs,
        )
        self._instances[key] = instance
        return instance

    def read_long_term(self) -> str:
        return self.legacy.read_long_term()

    def write_long_term(self, content: str) -> None:
        self.legacy.write_long_term(content)

    def append_history(self, entry: str) -> None:
        self.legacy.append_history(entry)

    def get_memory_context(self) -> str:
        return self.legacy.get_memory_context()

    def add_turn(self, user_input: str, assistant_response: str, session_key: str | None = None) -> None:
        if not self._enabled:
            return
        try:
            instance = self._get_instance(session_key)
            instance.add_memory(user_input=user_input, agent_response=assistant_response)
        except Exception:
            # Fail open: memory backend errors must not break response path.
            self._enabled = False
