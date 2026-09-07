# Technical Documentation

English | [简体中文](TECHNICAL.zh-CN.md)

## 1. Goal and boundary

The project treats stable leading `system`/`developer` messages and `tools` as a reusable prefix. User messages, assistant responses, tool results, and conversation history are excluded from generic prefix snapshots.

The middleware does not read GPU memory or run inference. It uses llama.cpp HTTP slot APIs for prefill, save, restore, and transparent forwarding.

## 2. Request path

```text
Client -> middleware.py/uvicorn -> app.py
       -> prefix extraction and hash
       -> /apply-template
       -> /completion (n_predict=0)
       -> /slots/{id}?action=save or restore
       -> /v1/chat/completions
       -> Client
```

`middleware.py` starts uvicorn and the interactive console. `app.py` owns configuration, HTTP endpoints, cache lifecycle, and the upstream proxy.

## 3. Prefix extraction

`agent_prefix(payload)` scans `messages` from the beginning. Consecutive `system` and `developer` messages become the prefix; scanning stops at the first other role. `tools` always belongs to the prefix. Dynamic workspace lines are removed before hashing and forwarding.

The normalized object is encoded as compact, sorted JSON and hashed with SHA-256. The same template produces the same `prefix_hash`; changes to tool order, schemas, or prompts produce a different hash.

## 4. Snapshot creation

For a new template the middleware:

1. Reads `/props` for model identity and slot count.
2. Allocates an idle slot, evicting an idle binding when necessary.
3. Calls `/apply-template` to obtain the exact llama.cpp-rendered prefix.
4. Calls `/completion` for the prefix with `n_predict=0`.
5. Saves the slot.
6. Marks the cache `ready` only when `n_saved == token_count` and the snapshot exists and is non-empty.

Metadata is stored at `data/prefixes/<id>.json`, the human-readable template reference at `data/templates/<id>.template.json`, and the binary snapshot is written by llama.cpp to `slot_save_path`.

## 5. Restore and hit verification

Before restoring, the middleware checks:

- status is `ready`;
- `snapshot_scope` is `prefix_only_v2`;
- saved token count equals prefix token count;
- the resolved path stays inside configured `slot_save_path`;
- request prefix hash matches metadata.

The restore response's `n_restored` confirms restore size. A final hit must be judged from response statistics. The middleware checks `usage.prompt_tokens_details.cached_tokens`, compatible OpenAI `usage` aliases, and then common llama.cpp `timings.cache_n`/`n_cache` fields. If the response contains no cache count, it reports an unknown result instead of inferring a hit from the restore request.

## 6. Slot rotation

With one slot, a new template selects in this order:

1. an upstream `/slots` idle slot with no metadata;
2. an idle slot with bindings, evicting the least recently used binding.

Only the process-local `loaded_slot_prefixes` binding is removed. Old metadata, template files, and binary snapshots stay on disk. A slot with `is_processing=true` is never evicted.

## 7. Concurrency model

KV requests use a process-local `asyncio.Lock` for the full lifecycle: restore, forwarding, reading a normal response, or consuming an entire SSE stream. With `--parallel 1`, one request cannot be replaced by another template while llama.cpp is processing it.

The lock is not cross-process. One middleware process must own a given set of llama.cpp slots, and clients must not bypass it to send requests directly to those slots.

## 8. Configuration and safety

`load_config()` reads YAML at startup and validates:

- the root and every section are mappings;
- the upstream address is an absolute `http`/`https` URL;
- the timeout is positive;
- the Agent port is in `1..65535`;
- the storage directory is non-empty.

Snapshot filenames are resolved under the configured snapshot directory to prevent path traversal. The default bind address is loopback. Authentication, TLS, and multi-tenant isolation are not included.

## 9. Failure and recovery

- unreachable llama.cpp: return 502 while the middleware remains alive;
- unsupported save/restore: use memory-only mode or return a clear error, never fake `ready`;
- missing snapshot: record the error and rebuild;
- corrupt metadata: return 500 with the filename;
- processing slot: return 409 or wait for another request to release the lock.

## 10. Test strategy

`tests/test_core.py` covers prefix boundaries, path restrictions, delete cleanup, migration restore, and configuration validation. CI runs compilation and tests on Python 3.10 and 3.12. Compatibility with a real model still needs validation against the target llama.cpp build.
