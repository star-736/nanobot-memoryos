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

            # Build the most recent user/assistant QA pairs from prompt history.
            # Retrieved mid-term pages that duplicate one of these recent turns
            # are skipped to avoid injecting the same exchange twice.
            recent_qa_pairs = self._extract_recent_qa_pairs(recent_history or [], limit=10)

            filtered_pages: list[dict[str, Any]] = []
            for page in pages:
                user_input = page.get("user_input", "")
                agent_response = page.get("agent_response", "")
                if not self._normalize_text(user_input) and not self._normalize_text(agent_response):
                    continue
                if self._page_matches_recent_qa(user_input, agent_response, recent_qa_pairs):
                    continue
                filtered_pages.append(page)

            recent_texts = [
                self._normalize_text(m.get("content", ""))
                for m in (recent_history or [])
                if isinstance(m, dict) and m.get("content")
            ]
            recent_texts = [t for t in recent_texts if t]

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

    @classmethod
    def _matches_text(cls, left: str, right: str) -> bool:
        left_norm = cls._normalize_text(left)
        right_norm = cls._normalize_text(right)
        if not left_norm or not right_norm:
            return False
        return (
            left_norm == right_norm
            or left_norm in right_norm
            or right_norm in left_norm
        )

    @classmethod
    def _extract_recent_qa_pairs(
        cls,
        history: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        pending_user: str | None = None
        pending_assistant: str | None = None

        for message in history:
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                content = "\n".join(part for part in text_parts if part)
            elif not isinstance(content, str):
                content = str(content or "")

            normalized = cls._normalize_text(content)
            if not normalized:
                continue

            if role == "user":
                if pending_user is not None and pending_assistant is not None:
                    pairs.append((pending_user, pending_assistant))
                pending_user = content
                pending_assistant = None
                continue

            if role != "assistant":
                continue

            # Skip intermediate assistant tool-call stubs; we only want final text replies.
            if message.get("tool_calls"):
                continue

            if pending_user is not None:
                pending_assistant = content

        if pending_user is not None and pending_assistant is not None:
            pairs.append((pending_user, pending_assistant))

        return pairs[-limit:]

    @classmethod
    def _page_matches_recent_qa(
        cls,
        user_input: str,
        agent_response: str,
        recent_qa_pairs: list[tuple[str, str]],
    ) -> bool:
        for recent_user, recent_assistant in recent_qa_pairs:
            if cls._matches_text(user_input, recent_user) and cls._matches_text(agent_response, recent_assistant):
                return True
        return False

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
