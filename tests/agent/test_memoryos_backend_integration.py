from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import ContextBuilder, TranscriptInput
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory_backend import LegacyMemoryBackend, MemoryOSBackend
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config, MemoryConfig
from nanobot.providers.base import GenerationSettings, LLMResponse


def make_loop(tmp_path, **kwargs):
    provider = MagicMock()
    provider.get_default_model.return_value = 'test-model'
    provider.generation = GenerationSettings()
    provider.api_key = 'provider-key'
    provider.api_base = 'https://provider.test/v1'
    provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(content='answer', usage=None))
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, **kwargs)


def test_memoryos_config_accepts_camel_case_keys():
    cfg = Config.model_validate({'memory': {'backend': 'memoryos', 'memoryos': {
        'memoryScope': 'global', 'memoryUserId': 'owner',
        'openaiApiKey': 'memory-key', 'openaiBaseUrl': 'https://memory.test/v1',
    }}})
    assert cfg.memory.backend == 'memoryos'
    assert cfg.memory.memoryos.memory_scope == 'global'
    assert cfg.memory.memoryos.openai_api_key == 'memory-key'


@pytest.mark.parametrize('include_memory', [True, False])
def test_transcript_respects_memory_policy(tmp_path, include_memory):
    builder = ContextBuilder(tmp_path)
    builder.memory.write_memory('file fact')
    messages = builder.build_transcript(
        TranscriptInput(history=[], current_message='question'),
        include_memory=include_memory, retrieved_memory='retrieved fact',
    )
    assert ('retrieved fact' in messages[0]['content']) is include_memory
    assert ('file fact' in messages[0]['content']) is include_memory
    assert messages[-1]['content'] == 'question'


@pytest.mark.asyncio
async def test_real_turn_retrieves_and_persists_memory(tmp_path):
    loop = make_loop(tmp_path)
    loop.memory_backend = MagicMock(spec=LegacyMemoryBackend)
    loop.memory_backend.retrieve_context.return_value = 'remembered preference'
    result = await loop.process_direct('question', session_key='cli:alice')
    assert result.content == 'answer'
    loop.memory_backend.retrieve_context.assert_called_once_with(
        'question', session_key='cli:alice', recent_history=[],
    )
    loop.memory_backend.add_turn.assert_called_once_with('question', 'answer', session_key='cli:alice')
    messages = loop.provider.chat_stream_with_retry.await_args.kwargs['messages']
    assert 'remembered preference' in messages[0]['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('transient', [False, True])
async def test_private_turn_neither_reads_nor_writes_memory(tmp_path, transient):
    loop = make_loop(tmp_path)
    loop.context.memory.write_memory('private file fact')
    loop.memory_backend = MagicMock(spec=LegacyMemoryBackend)
    if transient:
        loop.sessions.get_or_create_transient('cli:private')
    await loop.process_direct('question', session_key='cli:private', ephemeral=not transient)
    loop.memory_backend.retrieve_context.assert_not_called()
    loop.memory_backend.add_turn.assert_not_called()
    messages = loop.provider.chat_stream_with_retry.await_args.kwargs['messages']
    assert 'private file fact' not in str(messages)


def test_memoryos_expands_configured_storage_path(tmp_path, monkeypatch):
    captured = {}
    class FakeMemoryos:
        def __init__(self, **kwargs):
            captured.update(kwargs)
    fake_module = types.ModuleType('nanobot.memoryos_core.memoryos')
    fake_module.Memoryos = FakeMemoryos
    monkeypatch.setitem(sys.modules, 'nanobot.memoryos_core.memoryos', fake_module)
    backend = MemoryOSBackend(tmp_path, default_model='model', api_key='key',
                              memoryos_config={'data_storage_path': '~/memoryos-test'})
    backend._get_instance('session')
    assert '~' not in captured['data_storage_path']


def test_explicit_memory_credentials_survive_runtime_switch(tmp_path):
    backend = MemoryOSBackend(tmp_path, default_model='a', api_key='memory-key',
        api_base='https://memory.test/v1', memoryos_config={
            'openai_api_key': 'memory-key', 'openai_base_url': 'https://memory.test/v1',
        })
    backend.update_runtime(default_model='b', api_key='provider-key', api_base='https://provider.test/v1')
    assert backend.api_key == 'memory-key'
    assert backend.api_base == 'https://memory.test/v1'
    assert backend.default_model == 'b'


@pytest.mark.asyncio
async def test_failed_memoryos_keeps_file_memory_and_chat_working(tmp_path, monkeypatch):
    loop = make_loop(tmp_path, memory_config=MemoryConfig(backend='memoryos'))
    assert isinstance(loop.memory_backend, MemoryOSBackend)
    loop.context.memory.write_memory('durable file fact')
    monkeypatch.setattr(loop.memory_backend, '_get_instance', MagicMock(side_effect=RuntimeError('offline')))
    result = await loop.process_direct('question')
    assert result.content == 'answer'
    messages = loop.provider.chat_stream_with_retry.await_args.kwargs['messages']
    assert 'durable file fact' in messages[0]['content']
    assert not loop.memory_backend._enabled
    assert loop.sessions.get_or_create('cli:direct').messages[-1]['content'] == 'answer'


def test_memoryos_deduplicates_recent_dialogue(tmp_path, monkeypatch):
    backend = MemoryOSBackend(tmp_path, default_model='model', api_key='key')
    instance = MagicMock()
    instance.retriever.retrieve_context.return_value = {
        'retrieved_pages': [
            {'user_input': 'old question', 'agent_response': 'old answer'},
            {'user_input': 'favorite color', 'agent_response': 'blue'},
        ],
    }
    instance.user_long_term_memory.get_raw_user_profile.return_value = ''
    monkeypatch.setattr(backend, '_get_instance', lambda key: instance)
    text = backend.retrieve_context('color?', session_key='cli:alice', recent_history=[
        {'role': 'user', 'content': 'old question'}, {'role': 'assistant', 'content': 'old answer'},
    ])
    assert 'old question' not in text
    assert 'blue' in text


def test_whatsapp_is_not_started_when_disabled(tmp_path):
    config = Config.model_validate({'channels': {'whatsapp': {'enabled': False}}})
    manager = ChannelManager(config, MessageBus())
    assert 'whatsapp' not in manager.channels


@pytest.mark.asyncio
async def test_memoryos_config_reaches_loop_factory(tmp_path):
    config = Config.model_validate({
        'agents': {'defaults': {'workspace': str(tmp_path)}},
        'memory': {'backend': 'memoryos', 'memoryos': {'openaiApiKey': 'memory-key'}},
    })
    existing = make_loop(tmp_path)
    loop = AgentLoop.from_config(config, provider=existing.provider, model='test-model', tool_registry=existing.tools)
    assert isinstance(loop.memory_backend, MemoryOSBackend)
    assert loop.memory_backend.api_key == 'memory-key'
