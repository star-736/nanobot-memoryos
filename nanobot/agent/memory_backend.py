"""Memory backend abstractions for pluggable memory systems."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

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

    def retrieve_context(
        self,
        query: str,
        session_key: str | None = None,
        recent_history: list[dict[str, Any]] | None = None,
    ) -> str:
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
        # Keep legacy long-term content updated for future backend switches.
        return self.legacy.read_long_term()

    def write_long_term(self, content: str) -> None:
        # Mirror writes to legacy file memory for compatibility.
        self.legacy.write_long_term(content)

    def append_history(self, entry: str) -> None:
        # Mirror writes to legacy file history for compatibility.
        self.legacy.append_history(entry)

    def get_memory_context(self) -> str:
        # In MemoryOS mode, do not inject legacy # Memory block into prompt.
        return ""

    def add_turn(self, user_input: str, assistant_response: str, session_key: str | None = None) -> None:
        if not self._enabled:
            return
        try:
            instance = self._get_instance(session_key)
            instance.add_memory(user_input=user_input, agent_response=assistant_response)
        except Exception:
            # Fail open: memory backend errors must not break response path.
            self._enabled = False

    def retrieve_context(
        self,
        query: str,
        session_key: str | None = None,
        recent_history: list[dict[str, Any]] | None = None,
    ) -> str:
        if not self._enabled or not query.strip():
            return ""
        try:
            instance = self._get_instance(session_key)
            user_id = self._normalize_key(session_key)
            result = instance.retriever.retrieve_context(user_query=query, user_id=user_id)

            profile = instance.user_long_term_memory.get_raw_user_profile(user_id)
            pages = result.get("retrieved_pages", [])
            user_knowledge = result.get("retrieved_user_knowledge", [])
            assistant_knowledge = result.get("retrieved_assistant_knowledge", [])

            # Build a normalized recent-context set to reduce duplicate injection:
            # if retrieval text is already present in the current conversation window,
            # skip it to avoid prompt bloat/repetition.
            recent_texts = [
                self._normalize_text(m.get("content", ""))
                for m in (recent_history or [])
                if isinstance(m, dict) and m.get("content")
            ]
            recent_texts = [t for t in recent_texts if t]

            filtered_pages: list[dict[str, Any]] = []
            for page in pages:
                user_input = page.get("user_input", "")
                agent_response = page.get("agent_response", "")
                key = self._normalize_text(f"{user_input}\n{agent_response}")
                if not key:
                    continue
                if self._is_redundant_with_recent(key, recent_texts):
                    continue
                filtered_pages.append(page)

            filtered_user_knowledge: list[dict[str, Any]] = []
            for item in user_knowledge:
                knowledge = self._normalize_text(item.get("knowledge", ""))
                if not knowledge:
                    continue
                if self._is_redundant_with_recent(knowledge, recent_texts):
                    continue
                filtered_user_knowledge.append(item)

            filtered_assistant_knowledge: list[dict[str, Any]] = []
            for item in assistant_knowledge:
                knowledge = self._normalize_text(item.get("knowledge", ""))
                if not knowledge:
                    continue
                if self._is_redundant_with_recent(knowledge, recent_texts):
                    continue
                filtered_assistant_knowledge.append(item)

            parts: list[str] = []
            if profile and profile.lower() != "none":
                parts.append(f"## User Profile\n{profile}")

            if filtered_user_knowledge:
                lines = "\n".join(
                    f"- {k.get('knowledge', '')}" for k in filtered_user_knowledge if k.get("knowledge")
                )
                if lines:
                    parts.append(f"## User Knowledge\n{lines}")

            if filtered_assistant_knowledge:
                lines = "\n".join(
                    f"- {k.get('knowledge', '')}"
                    for k in filtered_assistant_knowledge
                    if k.get("knowledge")
                )
                if lines:
                    parts.append(f"## Assistant Knowledge\n{lines}")

            if filtered_pages:
                lines = []
                for page in filtered_pages:
                    user_input = page.get("user_input", "")
                    agent_response = page.get("agent_response", "")
                    if user_input or agent_response:
                        lines.append(f"- User: {user_input}\n  Assistant: {agent_response}")
                if lines:
                    parts.append("## Relevant Past Dialogues\n" + "\n".join(lines))

            return "\n\n".join(parts)
        except Exception:
            return ""

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _is_redundant_with_recent(candidate: str, recent_texts: list[str]) -> bool:
        if not candidate:
            return False
        for recent in recent_texts:
            if not recent:
                continue
            if candidate in recent or recent in candidate:
                return True
        return False
