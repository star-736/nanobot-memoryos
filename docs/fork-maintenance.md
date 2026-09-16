# Maintaining the MemoryOS fork

`origin` is `star-736/nanobot-memoryos`; `upstream` is `HKUDS/nanobot`.
Keep `main` on a verified integration and merge upstream on a temporary branch:

```bash
git fetch upstream main
git switch main
git switch -c sync/upstream-YYYYMMDD
git merge --no-commit --no-ff upstream/main
```

Resolve integration conflicts while retaining upstream behavior. The MemoryOS
implementation lives in `nanobot/memoryos_core/`, with its adapter in
`nanobot/agent/memory_backend.py`. `AgentLoop` retrieves once per run and stores
completed user turns at the persistence boundary. `ContextBuilder` only formats
the supplied retrieval result; token estimation cannot trigger backend calls.
The upstream file memory and Dream flow remain independent of backend failures.

Preserve WhatsApp source, tests, manifests, UI and documentation from upstream.
Disable the channel with `channels.whatsapp.enabled: false`, its default, rather
than deleting it. Existing MemoryOS data paths and session keys are not migrated
by this sync; a future key-format change needs an explicit data migration.

Run the focused regression suite before committing the merge:

```bash
python -m pytest tests/agent/test_memoryos_backend_integration.py tests/agent/test_context_prompt_cache.py tests/agent/test_loop_session_policy.py tests/agent/test_memory_store.py
python -m ruff check nanobot/agent/context.py nanobot/agent/loop.py nanobot/agent/memory_backend.py nanobot/config/schema.py tests/agent/test_memoryos_backend_integration.py
```

Also exercise the changed upstream interfaces (runner, context, configuration and
channel discovery). These offline tests use fake providers; they do not validate
a real embedding download, paid LLM calls or a Docker deployment.

After validation, commit the merge, fast-forward `main` to the sync branch and
push `main` to `origin`. Do not force-push or rewrite published main history.
Keep local utility scripts such as `core_agent_lines.ps1` out of the merge.
