"""Pluggable memory backend adapters."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.memory import MemoryStore


class LegacyMemoryBackend:
    """Compatibility adapter around nanobot's file-backed memory store."""

    def __init__(self, workspace: Path):
        self.store = MemoryStore(workspace)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    @property
    def git(self):
        return self.store.git

    def read_long_term(self) -> str:
        return self.store.read_memory()

    def write_long_term(self, content: str) -> None:
        self.store.write_memory(content)

    def append_history(self, entry: str, **kwargs: Any) -> int:
        return self.store.append_history(entry, **kwargs)

    def get_memory_context(self) -> str:
        return self.store.get_memory_context()

    def add_turn(self, user_input: str, assistant_response: str, session_key: str | None = None) -> None:
        return None

    def retrieve_context(
        self,
        query: str,
        session_key: str | None = None,
        recent_history: list[dict[str, Any]] | None = None,
    ) -> str:
        return ""


MemoryBackend = LegacyMemoryBackend


class MemoryOSBackend(LegacyMemoryBackend):
    """MemoryOS-backed memory adapter with safe fallback behavior."""

    def __init__(
        self,
        workspace: Path,
        *,
        default_model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        memoryos_config: dict[str, Any] | None = None,
    ):
        super().__init__(workspace)
        self.workspace = workspace
        self.default_model = default_model
        self.api_key = api_key or ""
        self.api_base = api_base or ""
        self.memoryos_config = memoryos_config or {}
        self._instances: dict[str, object] = {}
        self._enabled = True

    def update_runtime(
        self,
        *,
        default_model: str,
        api_key: str | None,
        api_base: str | None,
    ) -> None:
        next_api_key = api_key or self.api_key
        next_api_base = api_base or self.api_base
        if (
            default_model != self.default_model
            or next_api_key != self.api_key
            or next_api_base != self.api_base
        ):
            self._instances.clear()
        self.default_model = default_model
        self.api_key = next_api_key
        self.api_base = next_api_base

    def _resolve_memory_user_id(self, session_key: str | None) -> str:
        scope = str(self.memoryos_config.get("memory_scope") or "session").strip().lower()
        if scope == "global":
            return str(self.memoryos_config.get("memory_user_id") or "owner")
        return session_key or "default"

    def _normalize_key(self, session_key: str | None) -> str:
        memory_user_id = self._resolve_memory_user_id(session_key)
        if not memory_user_id:
            return "default"
        return memory_user_id.replace(":", "_").replace("/", "_").replace("\\", "_")

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
        configured_path = cfg.get("data_storage_path")
        data_storage_path = (
            str(Path(configured_path).expanduser())
            if configured_path
            else str(self.workspace / "memoryos_data")
        )
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

    def get_memory_context(self) -> str:
        return ""

    def add_turn(self, user_input: str, assistant_response: str, session_key: str | None = None) -> None:
        if not self._enabled:
            return
        try:
            instance = self._get_instance(session_key)
            instance.add_memory(user_input=user_input, agent_response=assistant_response)
        except Exception as exc:
            self._enabled = False
            logger.warning("MemoryOS ingest failed; falling back to file memory only: {}", exc)

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

            filtered_user_knowledge = [
                item for item in user_knowledge
                if self._normalize_text(item.get("knowledge", ""))
                and not self._is_redundant_with_recent(
                    self._normalize_text(item.get("knowledge", "")),
                    recent_texts,
                )
            ]
            filtered_assistant_knowledge = [
                item for item in assistant_knowledge
                if self._normalize_text(item.get("knowledge", ""))
                and not self._is_redundant_with_recent(
                    self._normalize_text(item.get("knowledge", "")),
                    recent_texts,
                )
            ]

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
        except Exception as exc:
            logger.warning("MemoryOS retrieval failed; omitting retrieved context: {}", exc)
            return ""

    @staticmethod
    def _normalize_text(text: Any) -> str:
        text = str(text or "").strip().lower()
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

            if not cls._normalize_text(content):
                continue
            if role == "user":
                if pending_user is not None and pending_assistant is not None:
                    pairs.append((pending_user, pending_assistant))
                pending_user = content
                pending_assistant = None
            elif role == "assistant" and not message.get("tool_calls") and pending_user is not None:
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
        return any(candidate in recent or recent in candidate for recent in recent_texts if recent)
