# llama.cpp KV Prefix Cache Middleware

English | [简体中文](README.md)

**Persistent llama.cpp KV prefix caching for OpenAI-compatible agents.**

[Technical design](docs/TECHNICAL.en.md) | [中文技术文档](docs/TECHNICAL.zh-CN.md) | [Contributing](CONTRIBUTING.en.md) | [Security](SECURITY.en.md) | [MIT License](LICENSE)

This middleware sits between an OpenAI-compatible agent and `llama.cpp`. It identifies the stable beginning of an agent request, pre-fills that prefix independently, saves the resulting slot KV state to disk, and restores it when a later conversation uses the same template.

The reusable prefix consists of leading `system` and `developer` messages plus the tool definitions. User messages and conversation history are deliberately excluded.

> Version 0.3.0 is an experimental release. Benchmark it with your own model, context configuration, llama.cpp build, and agent payload before production use.

## Why this project exists

Agent clients often resend a large fixed prompt on every new conversation:

- system and developer instructions;
- tool definitions and JSON Schemas;
- fixed capability descriptions;
- operational rules that rarely change.

That stable content may contain thousands of tokens. llama.cpp can reuse an in-memory prompt cache while its process remains alive, but a model restart clears the slot state. This project uses llama.cpp slot save/restore endpoints to make the stable prefix recoverable from disk.

## Measured result

In a documented Hermes Agent test, the middleware kept its KV snapshot, `llama.cpp` was restarted, and a new conversation was opened:

> **Average model processing time fell from 91.0 seconds to 13.7 seconds: 77.3 seconds less, or about 85.0% lower.**

| Scenario | Three model-processing times | Average |
| --- | --- | ---: |
| Transparent-routing cold start | 1:46, 1:24, 1:23 | **91.0 s** |
| Model restart with persisted KV restored | 0:12, 0:13, 0:16 | **13.7 s** |

The result demonstrates the middleware's intended behavior: stable agent prefixes such as system instructions and tool lists can be saved to disk and restored after a model restart or when a new conversation is opened. First-time KV creation averaged 1:29.0 and includes the one-time prefix-processing and snapshot-saving cost; the main gain appears during later restoration.

Full procedure, environment, result screenshots, and limitations:

- [KV prefix cache measured results (English)](docs/benchmarks/kv-prefix-cache/README.en.md)
- [KV 前缀缓存实测效果（中文）](docs/benchmarks/kv-prefix-cache/README.zh-CN.md)

```text
OpenAI-compatible Agent
          |
          | /v1/chat/completions
          v
KV Prefix Cache Middleware ---- metadata/templates ---- ./data
          |
          | restore / route / save
          v
      llama.cpp server -------- KV snapshots -------- slot-save-path
```

## Features

- OpenAI-compatible `/v1` proxy endpoints;
- automatic extraction and hashing of stable agent prefixes;
- prefix-only prefill before the user request is processed;
- disk-backed llama.cpp slot snapshots;
- automatic restore after a model restart or agent switch;
- idle slot eviction that preserves old snapshots;
- request serialization for `--parallel 1`, including streaming responses;
- runtime cache-hit statistics from llama.cpp responses;
- interactive management commands in the same terminal;
- Tab completion and Chinese/English console output;
- a small read-only status page and management API.

## Requirements

- Python 3.10 or later;
- a recent llama.cpp HTTP server build;
- a GGUF model compatible with that build;
- an OpenAI-compatible agent or client.

The llama.cpp build must expose:

- `/health`
- `/props`
- `/slots`
- `/slots/{id}?action=save`
- `/slots/{id}?action=restore`
- `/apply-template`
- `/completion`
- `/v1/chat/completions`

The middleware itself is platform-independent. GPU support, model loading, and inference acceleration remain llama.cpp responsibilities.

## Installation

```bash
git clone https://github.com/buhuo666/kv-middleware.git
cd llama-kv-middleware
python -m venv .venv
```

Activate the environment on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item config.example.yml config.yml
```

Activate it on Linux or macOS:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config.example.yml config.yml
```

For an editable local installation, use `python -m pip install -e .` instead of installing only from `requirements.txt`.

`config.yml` is intentionally ignored by Git because it can contain machine-specific addresses and paths.

## Start llama.cpp

Persistent snapshots require `--slot-save-path`:

```text
llama-server \
  -m /path/to/model.gguf \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 32768 \
  --parallel 1 \
  --slot-save-path /absolute/path/to/slot-cache \
  --metrics
```

Important options:

| Option | Purpose |
| --- | --- |
| `--slot-save-path` | Enables saving and restoring slot KV snapshots. Use the same directory in `config.yml`. |
| `--parallel 1` | Recommended for this release. Cached calls are serialized and snapshots are switched on the single slot. |
| `--ctx-size` | Must fit the prefix, current conversation, and generated output. |
| `--metrics` | Exposes counters useful for cache verification. |
| `--cache-type-k/v` | Optional KV quantization; keep it compatible when creating and restoring snapshots. |

Model paths, GPU layer options, and sampling parameters are local llama.cpp settings and are not part of this repository.

## Configure the middleware

Edit the local `config.yml`:

```yaml
llama_cpp:
  base_url: "http://llama-cpp-host:8080"
  timeout_seconds: 300
  slot_save_path: "/absolute/path/to/slot-cache"

agent:
  host: "127.0.0.1"
  port: 8000
  api_base: "http://127.0.0.1:8000/v1"

storage:
  data_dir: "./data"
```

Rules:

- `slot_save_path` must point to the directory passed to llama.cpp;
- `data_dir` stores metadata and template reference files, not persistent chat logs;
- configuration is loaded once at startup;
- `KV_CONFIG` may select another YAML file;
- `LLAMA_BASE_URL` and `KV_DATA_DIR` override their YAML values.

## Start the middleware

Start llama.cpp first, then run:

```bash
python middleware.py
```

The terminal becomes both the service process and its management console:

```text
llama.cpp KV Middleware 0.3.0
Agent API: http://127.0.0.1:8000/v1
kv>
```

Health and status endpoints:

```text
GET http://127.0.0.1:8000/health
GET http://127.0.0.1:8000/upstream/health
http://127.0.0.1:8000/admin
```

## Connect an agent

Replace the agent's direct llama.cpp base URL with the middleware URL:

```text
Base URL: http://127.0.0.1:8000/v1
API Key:  any non-empty placeholder, if the client requires one
```

Forwarded OpenAI-compatible endpoints include:

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/embeddings`, when supported upstream.

Normal agents do not need to select a cache ID. The middleware automatically builds, matches, restores, and replaces prefixes. Advanced clients may explicitly send:

```text
X-KV-Prefix-ID: <prefix-id>
X-KV-Slot-ID: 0
```

With explicit selection, the request template must match the saved prefix. A mismatch returns HTTP 409 instead of silently applying the wrong KV state.

## Cache lifecycle

### First request for a template

1. Extract leading system/developer messages and tools.
2. Render the exact model prompt with `/apply-template`.
3. Prefill only that reusable prefix with `n_predict: 0`.
4. Save the slot state to disk.
5. Forward the real request containing the user message.

The first request still pays the prefix prefill cost because it creates the reusable snapshot.

### Later request with the same template

If the slot already contains the target prefix, it is reused in memory. Otherwise the snapshot is restored before only the new conversation content is processed.

### Slot pressure

When a new template needs an idle slot, the active in-memory binding is evicted. Its metadata, template JSON, and binary snapshot stay on disk. If that agent returns later, its snapshot is restored.

A slot processing a model request is never replaced. With `--parallel 1`, cached calls wait their turn.

## Verify a cache hit

Use the console:

```text
logs
messages
```

Important events:

| Event | Meaning |
| --- | --- |
| `prefix_cache_built` | A prefix was prefilled and saved. |
| `prefix_slot_evicted` | The slot switched templates; the old disk snapshot remains. |
| `prefix_cache_restored` | A target snapshot was read from disk. |
| `prefix_cache_reused_in_memory` | The target prefix was already in the slot. |
| `prefix_cache_result` | llama.cpp's authoritative cache result. |
| `prefix_restore_failed_fallback` | Restore failed and the request entered fallback/rebuild handling. |

The authoritative result is `usage.prompt_tokens_details.cached_tokens` in the model response. It should be close to the saved prefix `token_count`. A restore event alone is not proof of a final hit.

llama.cpp builds expose this value in different places. The middleware checks
OpenAI `usage.prompt_tokens_details.cached_tokens` first, then compatible
`usage` aliases, and finally llama.cpp `timings.cache_n`/`n_cache` aliases. If
the upstream response contains no cache count, the console intentionally shows
`unknown` rather than inferring a hit from the restore request.

With `--metrics`, also inspect:

```text
llamacpp:prompt_tokens_total
llamacpp:prompt_tokens_cached_total
```

## Console commands

Command names do not start with a dash; options do. Press Tab at `kv>` for completion.

| Command | Description |
| --- | --- |
| `help` | Show commands and arguments. |
| `version` | Show version and process information. |
| `status` / `show` | Show middleware, routing, upstream, and cache status. |
| `config` | Show the startup configuration with supported secret fields redacted. |
| `list` | List cache ID, status, and creation time. |
| `list -f <value>` | Filter by cache ID, name, agent ID, filename, or date. |
| `del -f <value>` | Delete matching metadata, template, and snapshot. |
| `del all` | Explicitly delete every cache. This is destructive. |
| `messages` | Show request/response summaries retained by the current process. |
| `messages -f <number> [column]` | Show a full message or one selected field. |
| `messages clear` | Clear runtime message records. |
| `template` | List persisted agent template references. |
| `template -f <file-or-ID>` | Show one template. |
| `template del <file-or-ID>` | Delete one template reference. |
| `logs` | Show runtime logs. |
| `logs -f <number> [column]` | Show one log or one selected field. |
| `stop` | Disable cache behavior and keep transparent routing. |
| `start` | Enable cache behavior again. |
| `language chinese/english` | Change console language. |
| `exit` | Stop the service cleanly. |

## Runtime data

```text
data/
  prefixes/     cache metadata
  templates/    agent prefix references
  sessions/     migration metadata
```

llama.cpp writes binary KV snapshots to `slot_save_path`. Runtime `messages` and `logs` live only in memory and are cleared when the middleware exits.

Never commit local configuration, `data/`, snapshots, model files, logs, virtual environments, or agent templates. The included `.gitignore` covers the usual locations, but always inspect staged files before publishing.

## Compatibility and limitations

- A snapshot is generally tied to its model, llama.cpp build, context settings, KV types, and chat template. Rebuild caches after changing them.
- System prompts, tools, tool order, or JSON Schemas must match. A changed template receives a new cache.
- Some agents send title-generation or summarization requests with independent prefixes. They are cached separately.
- Concurrency protection is process-local. Do not run multiple middleware processes against the same llama.cpp slots.
- There is no authentication, TLS, rate limiting, or multi-tenant isolation in this release. Keep the default loopback binding unless a trusted gateway supplies those controls.
- Large snapshots consume substantial disk space, and restore latency depends on snapshot size and storage speed.
- The middleware cannot directly read GPU memory. Snapshot persistence is entirely provided by llama.cpp's slot API.

## Troubleshooting

### Cache remains `building`

Confirm that llama.cpp was started with `--slot-save-path`, that `config.yml` points to exactly the same directory, and that both processes can access it.

### Full prefill after a model restart

Check that the snapshot exists and is non-empty, then inspect `prefix_cache_restored` and the final `prefix_cache_result.cached_tokens`. Also verify that the model, context, chat template, KV types, system messages, and tool list have not changed.

### Miss after switching agents

Inspect the full restore log. Do not send requests directly to the same llama.cpp slot while the middleware manages it, because direct requests can invalidate the middleware's binding knowledge.

### Transparent routing only

Enter `stop` in the management console. Enter `start` to enable caching again.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

See [CONTRIBUTING.en.md](CONTRIBUTING.en.md) before opening a pull request. Security issues should follow [SECURITY.en.md](SECURITY.en.md), not public Issues.

Use the [release checklist](docs/RELEASE_CHECKLIST.md) before publishing a release.

## Security recommendations

- Keep the default loopback bind address unless a trusted gateway provides authentication and TLS.
- Do not expose `/admin` or `/admin/api/*` to an untrusted network.
- Treat snapshots, templates, and runtime message data as sensitive model-input data.
- Redact prompts, tool schemas, credentials, and machine paths before opening an Issue.

## Release and contribution

Read [CONTRIBUTING.en.md](CONTRIBUTING.en.md) before opening a pull request. Use
the [release checklist](docs/RELEASE_CHECKLIST.md) before publishing a release.

## Project status

Version 0.3.0 is an experimental implementation focused on persistent stable
agent-prefix snapshots, slot rotation, and recovery after a llama.cpp restart.
It is not a general conversation-memory system and does not inject old chat
history into a new conversation.

## License

MIT. See [LICENSE](LICENSE).
