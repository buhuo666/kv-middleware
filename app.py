from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("KV_CONFIG") or str(BASE_DIR / "config.yml")).resolve()


def load_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "llama_cpp": {"base_url": "http://127.0.0.1:8080", "timeout_seconds": 120},
        "agent": {"host": "127.0.0.1", "port": 8000},
        "storage": {"data_dir": "./data"},
    }
    if CONFIG_PATH.exists():
        try:
            loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"invalid YAML configuration: {CONFIG_PATH}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError("configuration root must be a YAML mapping")
        for section, values in loaded.items():
            if values is None:
                continue
            if not isinstance(values, dict):
                raise RuntimeError(f"configuration section '{section}' must be a mapping")
            defaults.setdefault(section, {}).update(values)
    if os.getenv("LLAMA_BASE_URL"):
        defaults["llama_cpp"]["base_url"] = os.environ["LLAMA_BASE_URL"]
    if os.getenv("KV_DATA_DIR"):
        defaults["storage"]["data_dir"] = os.environ["KV_DATA_DIR"]
    llama_config = defaults.get("llama_cpp")
    if not isinstance(llama_config, dict):
        raise RuntimeError("llama_cpp configuration must be a mapping")
    base_url = str(llama_config.get("base_url", "")).strip()
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RuntimeError("llama_cpp.base_url must be an absolute http:// or https:// URL")
    try:
        timeout = float(llama_config.get("timeout_seconds", 120))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("llama_cpp.timeout_seconds must be a positive number") from exc
    if timeout <= 0:
        raise RuntimeError("llama_cpp.timeout_seconds must be a positive number")
    llama_config["base_url"] = base_url
    llama_config["timeout_seconds"] = timeout
    agent_config = defaults.get("agent")
    if not isinstance(agent_config, dict):
        raise RuntimeError("agent configuration must be a mapping")
    try:
        port = int(agent_config.get("port", 8000))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("agent.port must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("agent.port must be an integer from 1 to 65535")
    agent_config["port"] = port
    storage_config = defaults.get("storage")
    if not isinstance(storage_config, dict) or not str(storage_config.get("data_dir", "")).strip():
        raise RuntimeError("storage.data_dir must be a non-empty path")
    return defaults


CONFIG = load_config()
LLAMA_BASE_URL = str(CONFIG["llama_cpp"]["base_url"]).rstrip("/")
LLAMA_TIMEOUT = float(CONFIG["llama_cpp"].get("timeout_seconds", 120))
DATA_DIR = Path(str(CONFIG["storage"]["data_dir"]))
if not DATA_DIR.is_absolute():
    DATA_DIR = (BASE_DIR / DATA_DIR).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "prefixes").mkdir(exist_ok=True)
(DATA_DIR / "templates").mkdir(exist_ok=True)
(DATA_DIR / "sessions").mkdir(exist_ok=True)

app = FastAPI(title="llama.cpp KV Middleware", version="0.3.0")
prefix_lock = asyncio.Lock()
runtime_messages: deque[dict[str, Any]] = deque(maxlen=200)
runtime_logs: deque[dict[str, Any]] = deque(maxlen=500)
middleware_enabled = True
route_requests = 0
route_errors = 0
loaded_slot_prefixes: dict[int, str] = {}
prefix_request_lock = asyncio.Lock()


async def serialize_prefix_requests(request: Request):
    """Keep a restored prefix bound to the slot until its model response ends.

    llama.cpp runs with ``--parallel 1`` in the supported deployment.  Without
    this request-scoped lock, two concurrent Agent calls can restore different
    snapshots into slot 0 between the restore call and the actual completion
    request, making one of the requests report a cache miss.  A yield
    dependency is finalized after a streaming response is consumed, so the
    slot remains reserved for the complete request lifecycle.
    """
    upstream_path = request.url.path
    if upstream_path not in {"/v1/chat/completions", "/v1/completions", "/completion"}:
        yield
        return
    if not middleware_enabled:
        yield
        return
    # Avoid serializing unrelated proxy traffic (for example /v1/models).  An
    # explicit prefix header always needs the lock; for automatic prefix
    # discovery, inspect the cached leading messages/tools from the request body.
    needs_prefix = bool(request.headers.get("x-kv-prefix-id"))
    if not needs_prefix and upstream_path in {"/v1/chat/completions", "/v1/completions"}:
        try:
            payload = json.loads((await request.body()) or b"{}")
            if isinstance(payload, dict):
                prefix_value, _ = agent_prefix(payload)
                needs_prefix = bool(prefix_value.get("messages") or prefix_value.get("tools"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if not needs_prefix:
        yield
        return
    await prefix_request_lock.acquire()
    try:
        yield
    finally:
        prefix_request_lock.release()


async def detect_upstream_slot_reset() -> None:
    """Forget in-memory slot bindings after llama.cpp has been restarted.

    ``loaded_slot_prefixes`` belongs to the middleware process, while the slot
    state belongs to the llama.cpp process.  Restarting only llama.cpp therefore
    used to leave a stale binding in this dictionary and caused the first request
    after the restart to skip the disk restore.  The slots endpoint exposes the
    last prompt length; a zero-length idle slot is the state of a fresh server.
    This check is deliberately best-effort: if the endpoint is unavailable, the
    normal request path will still report the upstream error.
    """
    if not loaded_slot_prefixes:
        return
    try:
        response = await llama("GET", "/slots", timeout=5.0)
        slots = response.json()
        if not isinstance(slots, list):
            return
        by_id = {
            int(slot.get("id")): slot
            for slot in slots
            if isinstance(slot, dict) and str(slot.get("id", "")).isdigit()
        }
    except (HTTPException, TypeError, ValueError, json.JSONDecodeError):
        return
    reset_slots: list[int] = []
    for slot_id in list(loaded_slot_prefixes):
        slot = by_id.get(slot_id)
        if slot is None:
            reset_slots.append(slot_id)
            continue
        try:
            raw_prompt_tokens = next(
                (slot.get(key) for key in ("n_prompt_tokens", "n_cache_tokens", "n_past", "n_tokens") if slot.get(key) is not None),
                0,
            )
            prompt_tokens = int(raw_prompt_tokens or 0)
        except (TypeError, ValueError):
            prompt_tokens = 0
        if prompt_tokens <= 0 and not slot.get("is_processing"):
            reset_slots.append(slot_id)
    for slot_id in reset_slots:
        prefix_id = loaded_slot_prefixes.pop(slot_id, None)
        runtime_event(
            "info",
            "upstream_slot_reset_detected",
            slot_id=slot_id,
            previous_prefix_id=prefix_id,
        )


async def restore_slot_prefix(meta: dict[str, Any], slot_id: int) -> dict[str, Any]:
    """Restore a disk snapshot and return llama.cpp's authoritative result."""
    response = await llama(
        "POST",
        f"/slots/{slot_id}?action=restore",
        json={"filename": meta["snapshot_filename"]},
    )
    try:
        result = response.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "llama.cpp returned an invalid slot restore response") from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "llama.cpp returned an invalid slot restore response")
    restored = result.get("n_restored")
    try:
        expected = int(meta.get("token_count") or 0)
    except (TypeError, ValueError):
        expected = 0
    if restored is not None:
        try:
            restored = int(restored)
        except (TypeError, ValueError) as exc:
            raise HTTPException(502, "llama.cpp returned an invalid n_restored value") from exc
        if expected > 0 and restored < expected:
            raise HTTPException(
                502,
                f"llama.cpp restored only {restored} of {expected} prefix tokens",
            )
    return result


def snapshot_is_prefix_only(meta: dict[str, Any]) -> bool:
    """Return whether metadata proves the snapshot contains only the prefix."""
    if meta.get("snapshot_scope") != "prefix_only_v2":
        return False
    try:
        expected = int(meta.get("token_count") or 0)
        snapshot_result = meta.get("snapshot_result") or {}
        if not isinstance(snapshot_result, dict):
            return False
        saved = int(snapshot_result.get("n_saved") or 0)
    except (TypeError, ValueError):
        return False
    # A snapshot with more tokens than the template was made after a user turn
    # and must not be reused as a supposedly generic Agent prefix.
    return expected > 0 and saved == expected


def runtime_event(level: str, event: str, **details: Any) -> None:
    runtime_logs.append({"time": time.time(), "level": level, "event": event, **details})


def response_cache_stats(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    usage = response.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = (
        details.get("cached_tokens")
        if isinstance(details, dict) else None
    )
    if cached is None:
        for key in (
            "cached_tokens",
            "cache_read_input_tokens",
            "prompt_tokens_cached",
            "cache_tokens",
        ):
            if usage.get(key) is not None:
                cached = usage.get(key)
                break
    # llama.cpp versions do not all expose the OpenAI ``usage`` extension.
    # Recent builds report the number of reused prompt tokens in ``timings``;
    # accept the known spellings without treating ``prompt_n`` (tokens newly
    # evaluated) as a cache hit.
    if cached is None:
        timings = response.get("timings") or {}
        if isinstance(timings, dict):
            for key in ("cached_tokens", "cache_n", "n_cache", "n_cached", "cache_tokens"):
                if timings.get(key) is not None:
                    cached = timings.get(key)
                    break
    result = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": cached,
    }
    if cached is not None:
        result["cache_hit"] = isinstance(cached, (int, float)) and cached > 0
    return result


def merge_cache_stats(record: dict[str, Any], stats: dict[str, Any]) -> None:
    for key, value in stats.items():
        if value is not None:
            # llama.cpp is authoritative. A restored slot is only a restore
            # attempt; the response decides whether its token prefix matched.
            record[key] = value


def clear_binding_after_cache_miss(prefix_meta: dict[str, Any] | None, record: dict[str, Any] | None) -> None:
    """Do not keep claiming a slot is loaded when llama.cpp reports no prefix hit."""
    if not prefix_meta or not record:
        return
    try:
        expected = int(prefix_meta.get("token_count") or 0)
        cached = int(record.get("cached_tokens"))
        slot_id = int(prefix_meta.get("slot_id"))
    except (TypeError, ValueError):
        return
    if expected <= 0 or cached >= expected:
        return
    prefix_id = prefix_meta.get("id")
    if loaded_slot_prefixes.get(slot_id) == prefix_id:
        loaded_slot_prefixes.pop(slot_id, None)
        runtime_event(
            "warning",
            "prefix_cache_miss_binding_cleared",
            prefix_id=prefix_id,
            slot_id=slot_id,
            expected_cached_tokens=expected,
            cached_tokens=cached,
        )


def response_text_cache_stats(text: str) -> dict[str, Any]:
    """Extract usage from an OpenAI-compatible SSE capture when present."""
    stats: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            stats.update(response_cache_stats(json.loads(raw)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return stats


def extract_user_question(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages") or []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
            return "\n".join(parts) or json.dumps(content, ensure_ascii=False)
        return str(content)
    return None


def slot_snapshot_file(filename: str | None) -> Path | None:
    root_value = CONFIG.get("llama_cpp", {}).get("slot_save_path")
    if not filename or not root_value:
        return None
    root = Path(str(root_value))
    if not root.is_absolute():
        root = BASE_DIR / root
    root = root.resolve()
    candidate = (root / filename).resolve()
    if root != candidate.parent:
        raise HTTPException(400, "invalid snapshot filename")
    return candidate


def validate_prefix_id(prefix_id: str) -> str:
    """Accept only generated/cache-safe identifiers in filesystem paths."""
    if not isinstance(prefix_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prefix_id):
        raise HTTPException(400, "invalid prefix id")
    return prefix_id


def repair_cache_states() -> None:
    now = time.time()
    for path in (DATA_DIR / "prefixes").glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                continue
            status = meta.get("status")
            if status not in {"building", "failed"}:
                continue
            snapshot = slot_snapshot_file(meta.get("snapshot_filename"))
            if (
                snapshot
                and snapshot.is_file()
                and snapshot.stat().st_size > 0
                and snapshot_is_prefix_only(meta)
            ):
                meta["status"] = "ready"
                meta["snapshot_size"] = snapshot.stat().st_size
                meta["repaired_at"] = time.time()
                meta.pop("save_error", None)
                atomic_json(path, meta)
            elif status == "failed":
                # A failed record must not become ready merely because a stale
                # or user-inclusive snapshot happens to be present.
                continue
            elif now - float(meta.get("created_at", now)) > 300:
                meta["status"] = "failed"
                meta["save_error"] = "previous save did not produce a verified prefix-only snapshot"
                meta["failed_at"] = time.time()
                atomic_json(path, meta)
        except (OSError, TypeError, ValueError, HTTPException):
            continue


repair_cache_states()

ADMIN_HTML = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>KV Middleware 管理台</title><style>body{font:15px Segoe UI,Arial;max-width:1000px;margin:32px auto;padding:0 18px;background:#f6f8fb;color:#202124}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.card{background:white;border:1px solid #dfe3e8;border-radius:8px;padding:16px}.label{color:#68707d;font-size:13px}.value{font-size:20px;margin:6px 0}.ok{color:#137333}.bad{color:#b3261e}pre{white-space:pre-wrap;word-break:break-word;font-size:12px}</style><h1>llama.cpp KV Middleware 管理台</h1><p><button onclick='load()'>刷新状态</button> <span id='time'></span></p><div id='cards' class='grid'></div><div class='card'><h3>启动配置</h3><pre id='config'>加载中...</pre></div><script>async function load(){let s=await fetch('/admin/api/status').then(r=>r.json()),c=await fetch('/admin/api/config').then(r=>r.json());document.querySelector('#cards').innerHTML='<div class="card"><div class="label">服务</div><div class="value ok">'+s.service.status+'</div><pre>version: '+s.service.version+'\npid: '+s.service.pid+'</pre></div><div class="card"><div class="label">llama.cpp</div><div class="value '+(s.upstream.connected?'ok':'bad')+'">'+(s.upstream.connected?'已连接':'未连接')+'</div><pre>'+s.config.llama_base_url+'</pre></div><div class="card"><div class="label">存储</div><div class="value">'+s.storage.bytes+' bytes</div><pre>prefixes: '+s.storage.prefix_count+'\nsessions: '+s.storage.session_count+'</pre></div><div class="card"><div class="label">OpenAI 接口</div><div class="value">/v1</div><pre>/v1/models\n/v1/chat/completions</pre></div>';document.querySelector('#config').textContent=JSON.stringify(c,null,2);document.querySelector('#time').textContent=new Date().toLocaleString()}load();setInterval(load,10000)</script>"""


@app.get("/admin", include_in_schema=False)
async def admin_page() -> Response:
    return Response(ADMIN_HTML, media_type="text/html; charset=utf-8")


@app.get("/admin/api/status")
async def admin_status() -> dict[str, Any]:
    global route_requests, route_errors
    upstream: dict[str, Any]
    try:
        async with httpx.AsyncClient(base_url=LLAMA_BASE_URL, timeout=5.0) as client:
            response = await client.get("/health")
            response.raise_for_status()
            upstream = {"connected": True, "health": response.json()}
    except Exception as exc:
        upstream = {"connected": False, "error": str(exc)}
    prefixes = list((DATA_DIR / "prefixes").glob("*.json"))
    sessions = list((DATA_DIR / "sessions").glob("*.json"))
    storage_bytes = 0
    for path in DATA_DIR.rglob("*"):
        try:
            if path.is_file():
                storage_bytes += path.stat().st_size
        except OSError:
            # A concurrent delete or rotation should not make the status page
            # fail; the next refresh will report the new total.
            continue
    return {
        "service": {"status": "ok", "version": app.version, "pid": os.getpid(), "middleware_enabled": middleware_enabled},
        "config": {"config_path": str(CONFIG_PATH), "data_dir": str(DATA_DIR), "llama_base_url": LLAMA_BASE_URL},
        "upstream": upstream,
        "storage": {"prefix_count": len(prefixes), "session_count": len(sessions), "bytes": storage_bytes},
        "routing": {"enabled": True, "requests": route_requests, "errors": route_errors},
        "slot_bindings": {str(slot_id): prefix_id for slot_id, prefix_id in loaded_slot_prefixes.items()},
        "slot_eviction": {
            "enabled": True,
            "policy": "idle_lru_preserve_snapshots",
            "note": "旧 KV 只从内存 slot 淘汰，磁盘快照保留；正在处理的 slot 不会淘汰",
        },
        "endpoints": {"openai_base": "/v1", "admin": "/admin"},
    }


@app.get("/admin/api/config")
async def admin_config() -> dict[str, Any]:
    # Keep the management endpoint useful while preventing a future config
    # extension (password, token, authorization, etc.) from being echoed back.
    sensitive_markers = ("key", "secret", "password", "token", "authorization", "credential")

    def redact(value: Any, key: str = "") -> Any:
        if any(marker in key.casefold() for marker in sensitive_markers):
            return "***" if value else ""
        if isinstance(value, dict):
            return {name: redact(item, str(name)) for name, item in value.items()}
        if isinstance(value, list):
            return [redact(item, key) for item in value]
        return value

    return redact(json.loads(json.dumps(CONFIG)))


def cache_info(meta: dict[str, Any]) -> dict[str, Any]:
    result = dict(meta)
    result["metadata_file"] = str(prefix_path(meta["id"]))
    template = template_path(meta["id"])
    result["template_file"] = str(template)
    result["template_exists"] = template.is_file()
    result["template_size"] = template.stat().st_size if template.is_file() else 0
    snapshot = slot_snapshot_file(meta.get("snapshot_filename"))
    result["snapshot_file"] = str(snapshot) if snapshot else None
    result["snapshot_exists"] = bool(snapshot and snapshot.is_file())
    result["snapshot_size"] = snapshot.stat().st_size if snapshot and snapshot.is_file() else 0
    if result.get("status") == "building" and not result["snapshot_exists"]:
        result["size_status"] = "snapshot not generated"
    elif result.get("status") == "ready" and not result["snapshot_exists"]:
        result["size_status"] = "snapshot missing"
    else:
        result["size_status"] = "ok"
    return result


@app.get("/admin/api/caches")
async def admin_caches() -> list[dict[str, Any]]:
    return [cache_info(read_json(path)) for path in sorted((DATA_DIR / "prefixes").glob("*.json"))]


@app.get("/admin/api/messages")
async def admin_messages(limit: int = 50) -> list[dict[str, Any]]:
    return list(runtime_messages)[-max(1, min(limit, 200)):]


@app.delete("/admin/api/messages")
async def clear_admin_messages() -> dict[str, int]:
    count = len(runtime_messages)
    runtime_messages.clear()
    runtime_event("info", "messages_cleared", count=count)
    return {"deleted": count}


@app.get("/admin/api/logs")
async def admin_logs(limit: int = 100) -> list[dict[str, Any]]:
    return list(runtime_logs)[-max(1, min(limit, 500)):]


@app.get("/admin/api/templates")
async def admin_templates() -> list[dict[str, Any]]:
    templates = []
    for path in sorted((DATA_DIR / "prefixes").glob("*.json")):
        meta = read_json(path)
        template = load_template(meta)
        if template:
            templates.append(template)
    return templates


@app.get("/admin/api/templates/{prefix_id}")
async def admin_template(prefix_id: str) -> dict[str, Any]:
    meta = read_json(prefix_path(prefix_id))
    template = load_template(meta)
    if not template:
        raise HTTPException(404, "template file not found")
    return template


@app.delete("/admin/api/templates/{prefix_id}")
async def delete_admin_template(prefix_id: str) -> dict[str, str]:
    meta = read_json(prefix_path(prefix_id))
    template_path(prefix_id).unlink(missing_ok=True)
    meta["template_deleted_at"] = time.time()
    atomic_json(prefix_path(prefix_id), meta)
    runtime_event("info", "template_deleted", prefix_id=prefix_id)
    return {"prefix_id": prefix_id, "status": "deleted"}


class PrefixCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1)
    slot_id: int | None = Field(default=None, ge=0)
    create_snapshot: bool = True


class PrefixRestore(BaseModel):
    slot_id: int = Field(ge=0)


class MigrationRequest(BaseModel):
    session_id: str = Field(min_length=1)
    target_prefix_id: str = Field(min_length=1)
    mode: Literal["none", "full"] = "full"
    history: str = ""
    source_prefix_id: str | None = None
    slot_id: int | None = Field(default=None, ge=0)


class SnapshotPolicy(BaseModel):
    max_bytes: int = Field(default=10 * 1024 * 1024 * 1024, gt=0)
    max_snapshots: int = Field(default=1000, gt=0)


def normalized_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def remove_dynamic_context(text: str) -> str:
    """Remove per-workspace system-prompt lines before caching or hashing."""
    text = re.sub(r"(?im)^\s*Current workspace\s*:\s*.*(?:\r?\n|$)", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def hashes(text: str) -> dict[str, str]:
    raw = text.encode("utf-8")
    norm = normalized_text(text).encode("utf-8")
    return {"raw_sha256": hashlib.sha256(raw).hexdigest(), "normalized_sha256": hashlib.sha256(norm).hexdigest()}


def agent_prefix(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    leading = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            break
        if message.get("role") not in {"system", "developer"}:
            break
        copied = dict(message)
        if isinstance(copied.get("content"), str):
            cleaned = remove_dynamic_context(copied["content"])
            if cleaned:
                copied["content"] = cleaned
            else:
                continue
        leading.append(copied)
    value = {"messages": leading, "tools": payload.get("tools") or []}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return value, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sanitize_payload_prefix(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep forwarded leading messages byte-for-byte aligned with the cached prefix."""
    sanitized = dict(payload)
    messages = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            messages.append(message)
            continue
        if message.get("role") not in {"system", "developer"}:
            messages.append(message)
            continue
        copied = dict(message)
        if isinstance(copied.get("content"), str):
            cleaned = remove_dynamic_context(copied["content"])
            if cleaned:
                copied["content"] = cleaned
                messages.append(copied)
        else:
            messages.append(copied)
    sanitized["messages"] = messages
    return sanitized


async def evict_slot_for_prefix(
    existing: list[dict[str, Any]],
    total_slots: int,
    requested_slot: int | None = None,
) -> tuple[int, list[str]]:
    """Choose an idle slot for a new template without deleting old snapshots.

    A disk-backed prefix remains ``ready`` after eviction.  Only the process-local
    binding is removed; a later request for that prefix will restore its snapshot.
    """
    try:
        response = await llama("GET", "/slots", timeout=5.0)
        upstream_slots = response.json()
    except HTTPException as exc:
        raise HTTPException(503, f"cannot inspect llama.cpp slots for safe eviction: {exc.detail}") from exc
    if not isinstance(upstream_slots, list):
        raise HTTPException(503, "cannot inspect llama.cpp slots for safe eviction")
    upstream_ids = {
        int(slot.get("id"))
        for slot in upstream_slots
        if isinstance(slot, dict)
        and str(slot.get("id", "")).isdigit()
        and 0 <= int(slot.get("id")) < total_slots
    }
    busy = {
        int(slot.get("id"))
        for slot in upstream_slots
        if isinstance(slot, dict)
        and str(slot.get("id", "")).isdigit()
        and slot.get("is_processing")
    }
    by_slot: dict[int, list[dict[str, Any]]] = {}
    for item in existing:
        try:
            slot_id = int(item.get("slot_id"))
        except (TypeError, ValueError):
            continue
        if 0 <= slot_id < total_slots and item.get("status") in {"building", "memory_ready", "ready"}:
            by_slot.setdefault(slot_id, []).append(item)
    # Inspect every upstream slot, not only slots represented by our metadata.
    # A clean/idle slot with no prefix record is the safest place for a new
    # template and must not be mistaken for a full cache.
    if requested_slot is not None:
        slot_ids = [requested_slot]
    else:
        idle_slots = sorted(upstream_ids - busy)
        idle_without_metadata = [slot_id for slot_id in idle_slots if slot_id not in by_slot]
        idle_with_metadata = [slot_id for slot_id in idle_slots if slot_id in by_slot]
        slot_ids = idle_without_metadata + idle_with_metadata
    for slot_id in slot_ids:
        if slot_id is None or slot_id < 0 or slot_id >= total_slots:
            continue
        if upstream_ids and slot_id not in upstream_ids:
            continue
        if slot_id in busy:
            continue
        items = by_slot.get(slot_id, [])
        if not items:
            # A slot may be occupied in llama.cpp while its metadata is stale or
            # missing. Do not overwrite it unless the upstream reports it idle.
            loaded_slot_prefixes.pop(slot_id, None)
            runtime_event(
                "warning",
                "prefix_slot_reused_without_metadata",
                slot_id=slot_id,
                snapshot_preserved=True,
            )
            return slot_id, []
        active_id = loaded_slot_prefixes.get(slot_id)
        active_items = [item for item in items if item.get("id") == active_id]
        victim = active_items[0] if active_items else min(
            items,
            key=lambda item: float(item.get("last_used_at") or item.get("ready_at") or item.get("created_at") or 0),
        )
        evicted_ids = [str(item.get("id")) for item in items if item.get("id")]
        loaded_slot_prefixes.pop(slot_id, None)
        runtime_event(
            "warning",
            "prefix_slot_evicted",
            slot_id=slot_id,
            evicted_prefix_id=victim.get("id"),
            evicted_prefix_ids=evicted_ids,
            snapshot_preserved=True,
            reason="new Agent template requires a slot",
        )
        return slot_id, evicted_ids
    if requested_slot is not None:
        raise HTTPException(409, f"requested llama.cpp slot {requested_slot} is busy")
    raise HTTPException(409, "all llama.cpp slots are busy; no idle slot can be evicted")


async def resolve_agent_prefix(agent_id: str, payload: dict[str, Any], requested_slot: int | None) -> dict[str, Any]:
    value, fingerprint = agent_prefix(payload)
    if not value["messages"] and not value["tools"]:
        raise HTTPException(400, "Agent request has no system/developer message or tools to cache")
    async with prefix_lock:
        existing = [read_json(p) for p in (DATA_DIR / "prefixes").glob("*.json")]
        candidates = [item for item in existing if item.get("prefix_hash") == fingerprint]
        if candidates:
            # A previous interrupted request may have left a duplicate record with
            # the same template hash but a snapshot containing the user's answer as
            # well. Prefer a ready prefix-only snapshot whose saved token count is
            # exactly the cached template length, then the newest valid record.
            def candidate_score(item: dict[str, Any]) -> tuple[int, int, int, float]:
                try:
                    snapshot_result = item.get("snapshot_result") or {}
                    if not isinstance(snapshot_result, dict):
                        snapshot_result = {}
                    saved = int(snapshot_result.get("n_saved") or 0)
                    token_count = int(item.get("token_count") or 0)
                except (TypeError, ValueError):
                    saved, token_count = 0, 0
                exact_prefix = int(token_count > 0 and saved == token_count)
                return (
                    int(item.get("status") == "ready"),
                    exact_prefix,
                    int(item.get("snapshot_scope") == "prefix_only_v2"),
                    float(item.get("ready_at") or item.get("created_at") or 0),
                )
            selected = max(candidates, key=candidate_score)
            selected["last_used_at"] = time.time()
            atomic_json(prefix_path(selected["id"]), selected)
            return selected
        props = (await llama("GET", "/props")).json()
        if not isinstance(props, dict):
            raise HTTPException(502, "llama.cpp returned an invalid properties response")
        total_slots = int(props.get("total_slots", 1))
        if total_slots < 1:
            raise HTTPException(502, "llama.cpp reported no usable slots")
        used = {int(item["slot_id"]) for item in existing if item.get("status") in {"building", "memory_ready", "ready"} and item.get("slot_id") is not None}
        free: list[int] = []
        evicted_ids: list[str] = []
        if requested_slot is not None:
            if requested_slot in used:
                slot_id, evicted_ids = await evict_slot_for_prefix(existing, total_slots, requested_slot)
            else:
                slot_id = requested_slot
        else:
            free = [slot for slot in range(total_slots) if slot not in used]
            if free:
                slot_id = free[0]
            else:
                slot_id, evicted_ids = await evict_slot_for_prefix(existing, total_slots)
        if slot_id >= total_slots:
            raise HTTPException(400, f"slot {slot_id} is outside llama.cpp slot range 0..{total_slots - 1}")
        prefix_id = uuid.uuid4().hex
        meta = {
            "id": prefix_id, "agent_id": agent_id, "name": f"{agent_id}-{fingerprint[:8]}",
            "prefix": value, "prefix_hash": fingerprint, "slot_id": slot_id,
            "status": "building", "snapshot_filename": f"prefix-{prefix_id}.bin",
            "created_at": time.time(), "last_used_at": time.time(),
            "model_fingerprint": hashlib.sha256(str(props.get("model_path", "")).encode()).hexdigest(),
            "llama_cpp_version": props.get("build_info"),
        }
        if not free and evicted_ids:
            meta["slot_reuse"] = "evicted"
            meta["evicted_prefix_ids"] = evicted_ids
        write_template(meta)
        atomic_json(prefix_path(prefix_id), meta)
        return meta


async def save_agent_slot(meta: dict[str, Any]) -> None:
    runtime_event("info", "slot_save_started", prefix_id=meta.get("id"), slot_id=meta.get("slot_id"), filename=meta.get("snapshot_filename"))
    try:
        saved = await asyncio.wait_for(
            llama("POST", f"/slots/{meta['slot_id']}?action=save", json={"filename": meta["snapshot_filename"]}, timeout=LLAMA_TIMEOUT),
            timeout=max(LLAMA_TIMEOUT, 300.0),
        )
        try:
            snapshot_result = saved.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(502, "llama.cpp returned an invalid slot save response") from exc
        if not isinstance(snapshot_result, dict):
            raise HTTPException(502, "llama.cpp returned an invalid slot save response")
        meta["snapshot_result"] = snapshot_result
        snapshot = slot_snapshot_file(meta["snapshot_filename"])
        if snapshot is None or not snapshot.is_file() or snapshot.stat().st_size <= 0:
            meta["status"] = "failed"
            meta["save_error"] = "llama.cpp returned success but snapshot file was not found or is empty"
            atomic_json(prefix_path(meta["id"]), meta)
            runtime_event("error", "slot_save_missing_file", prefix_id=meta.get("id"), filename=meta.get("snapshot_filename"))
            return
        if not snapshot_is_prefix_only(meta):
            meta["status"] = "failed"
            meta["save_error"] = "snapshot was saved but metadata does not prove it contains only the reusable prefix"
            atomic_json(prefix_path(meta["id"]), meta)
            runtime_event("error", "slot_save_unverified_scope", prefix_id=meta.get("id"), filename=meta.get("snapshot_filename"))
            return
        meta["status"] = "ready"
        meta["snapshot_size"] = snapshot.stat().st_size
        meta["ready_at"] = time.time()
        write_template(meta)
        atomic_json(prefix_path(meta["id"]), meta)
        runtime_event("info", "slot_save_completed", prefix_id=meta.get("id"), size=meta.get("snapshot_size"))
    except HTTPException as exc:
        if exc.status_code == 501:
            meta["status"] = "memory_ready"
            meta["warning"] = "llama.cpp does not support persistent slot save; this prefix is usable only until the current model process exits"
            meta.pop("save_error", None)
            atomic_json(prefix_path(meta["id"]), meta)
            runtime_event("warning", "slot_save_unsupported_memory_only", prefix_id=meta.get("id"), slot_id=meta.get("slot_id"))
            return
        meta["status"] = "failed"
        meta["save_error"] = exc.detail
        atomic_json(prefix_path(meta["id"]), meta)
        runtime_event("error", "slot_save_failed", prefix_id=meta.get("id"), error=str(exc.detail))
    except asyncio.TimeoutError:
        meta["status"] = "failed"
        meta["save_error"] = "slot save timed out"
        atomic_json(prefix_path(meta["id"]), meta)
        runtime_event("error", "slot_save_timeout", prefix_id=meta.get("id"))


async def build_and_save_prefix(meta: dict[str, Any], payload: dict[str, Any]) -> None:
    """Prefill and persist only the reusable system/developer/tools prefix."""
    prefix = meta.get("prefix") or {}
    rendered = await llama(
        "POST",
        "/apply-template",
        json={
            "messages": prefix.get("messages") or [],
            "tools": prefix.get("tools") or [],
            "add_generation_prompt": False,
        },
        timeout=LLAMA_TIMEOUT,
    )
    try:
        rendered_payload = rendered.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "llama.cpp returned an invalid template response") from exc
    if not isinstance(rendered_payload, dict):
        raise HTTPException(502, "llama.cpp returned an invalid template response")
    prompt = rendered_payload.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise HTTPException(502, "llama.cpp returned an empty rendered prefix")
    meta["rendered_prefix"] = prompt
    prefill = {
        "prompt": prompt,
        "n_predict": 0,
        "temperature": 0,
        "stream": False,
        "id_slot": int(meta["slot_id"]),
        "cache_prompt": False,
    }
    result = await llama("POST", "/completion", json=prefill, timeout=LLAMA_TIMEOUT)
    try:
        details = result.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "llama.cpp returned an invalid prefill response") from exc
    if not isinstance(details, dict):
        raise HTTPException(502, "llama.cpp returned an invalid prefill response")
    timings = details.get("timings") or {}
    if not isinstance(timings, dict):
        timings = {}
    try:
        token_count = int(timings.get("prompt_n") or details.get("tokens_evaluated") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(502, "llama.cpp returned an invalid prefill token count") from exc
    if token_count <= 0:
        raise HTTPException(502, "llama.cpp returned no evaluated tokens for the prefix")
    meta["token_count"] = token_count
    meta["snapshot_scope"] = "prefix_only_v2"
    write_template(meta)
    atomic_json(prefix_path(meta["id"]), meta)
    await save_agent_slot(meta)


@app.post("/prefixes/{prefix_id}/save")
async def save_prefix(prefix_id: str) -> dict[str, Any]:
    meta = read_json(prefix_path(prefix_id))
    if meta.get("slot_id") is None or not meta.get("snapshot_filename"):
        raise HTTPException(409, "prefix has no assigned slot or snapshot filename")
    meta["status"] = "building"
    atomic_json(prefix_path(prefix_id), meta)
    await save_agent_slot(meta)
    return cache_info(read_json(prefix_path(prefix_id)))


def prefix_path(prefix_id: str) -> Path:
    return DATA_DIR / "prefixes" / f"{validate_prefix_id(prefix_id)}.json"


def template_path(prefix_id: str) -> Path:
    return DATA_DIR / "templates" / f"{validate_prefix_id(prefix_id)}.template.json"


def template_document(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Produce the durable, human-readable reference for one reusable prefix."""
    prefix = meta.get("prefix")
    if not isinstance(prefix, dict):
        return None
    return {
        "schema_version": "agent_template_v1",
        "filename": template_path(meta["id"]).name,
        "prefix_id": meta["id"],
        "agent_id": meta.get("agent_id"),
        "name": meta.get("name"),
        "prefix_hash": meta.get("prefix_hash"),
        "snapshot_scope": meta.get("snapshot_scope"),
        "model_fingerprint": meta.get("model_fingerprint"),
        "llama_cpp_version": meta.get("llama_cpp_version"),
        "created_at": meta.get("created_at"),
        "ready_at": meta.get("ready_at"),
        "token_count": meta.get("token_count"),
        "messages": prefix.get("messages") or [],
        "tools": prefix.get("tools") or [],
        "rendered_prefix": meta.get("rendered_prefix"),
    }


def write_template(meta: dict[str, Any]) -> Path | None:
    document = template_document(meta)
    if document is None:
        return None
    path = template_path(meta["id"])
    atomic_json(path, document)
    meta["template_file"] = str(path)
    meta["template_size"] = path.stat().st_size
    return path


def load_template(meta: dict[str, Any]) -> dict[str, Any] | None:
    if meta.get("template_deleted_at"):
        return None
    path = template_path(meta["id"])
    if path.is_file():
        document = read_json(path)
    else:
        # Existing caches predate separate template files. Create the reference on
        # demand without changing their snapshot or cache state.
        document = template_document(meta)
        if document is None:
            return None
        atomic_json(path, document)
    return document


def snapshot_path(prefix_id: str) -> Path:
    return DATA_DIR / "prefixes" / f"{prefix_id}.bin"


def session_path(session_id: str) -> Path:
    safe = hashlib.sha256(session_id.encode()).hexdigest()
    return DATA_DIR / "sessions" / f"{safe}.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, "resource not found")
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"invalid or unreadable metadata file: {path.name}") from exc
    if not isinstance(value, dict):
        raise HTTPException(500, f"metadata file is not a JSON object: {path.name}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


async def llama(method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        async with httpx.AsyncClient(base_url=LLAMA_BASE_URL, timeout=kwargs.pop("timeout", LLAMA_TIMEOUT)) as client:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 501:
            raise HTTPException(501, "llama.cpp does not support this slot operation") from exc
        raise HTTPException(502, f"llama.cpp request failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"llama.cpp request failed: {exc}") from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/upstream/health")
async def upstream_health() -> dict[str, Any]:
    """Check the connection from this middleware to the running llama.cpp server."""
    try:
        async with httpx.AsyncClient(base_url=LLAMA_BASE_URL, timeout=5.0) as client:
            response = await client.get("/health")
            response.raise_for_status()
            return {"status": "ok", "llama_base_url": LLAMA_BASE_URL, "upstream": response.json()}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"llama.cpp is unreachable at {LLAMA_BASE_URL}: {exc}") from exc


@app.post("/prefixes")
async def create_prefix(req: PrefixCreate) -> dict[str, Any]:
    prefix_id = uuid.uuid4().hex
    meta = {
        "id": prefix_id,
        "name": req.name,
        "text": req.text,
        "hashes": hashes(req.text),
        "slot_id": req.slot_id,
        "snapshot_filename": None,
        "status": "registered",
        "created_at": time.time(),
    }
    if req.create_snapshot:
        if req.slot_id is None:
            raise HTTPException(400, "slot_id is required when create_snapshot=true")
        try:
            props = (await llama("GET", "/props")).json()
            if not isinstance(props, dict):
                raise HTTPException(502, "llama.cpp returned an invalid properties response")
            total_slots = int(props.get("total_slots", 1))
            if total_slots < 1:
                raise HTTPException(502, "llama.cpp reported no usable slots")
            if req.slot_id < 0 or req.slot_id >= total_slots:
                raise HTTPException(400, f"slot {req.slot_id} is outside llama.cpp slot range 0..{total_slots - 1}")
            tokenized = (await llama("POST", "/tokenize", json={"content": req.text, "add_special": True})).json()
            if not isinstance(tokenized, dict):
                raise HTTPException(502, "llama.cpp returned an invalid tokenize response")
            token_ids = tokenized.get("tokens", [])
            if not isinstance(token_ids, list) or not token_ids:
                raise HTTPException(502, "llama.cpp returned no tokens for the prefix")
            meta["model_fingerprint"] = hashlib.sha256(str(props.get("model_path", "")).encode()).hexdigest()
            meta["tokenizer_fingerprint"] = hashlib.sha256(json.dumps(token_ids).encode()).hexdigest()
            meta["context_size"] = props.get("default_generation_settings", {}).get("n_ctx")
            meta["llama_cpp_version"] = props.get("build_info")
            meta["token_count"] = len(token_ids)
            meta["snapshot_scope"] = "prefix_only_v2"
            await llama("POST", "/completion", json={"prompt": req.text, "cache_prompt": True, "id_slot": req.slot_id, "n_predict": 0})
            filename = f"prefix-{prefix_id}.bin"
            async with httpx.AsyncClient(base_url=LLAMA_BASE_URL, timeout=LLAMA_TIMEOUT) as client:
                saved = await client.post(f"/slots/{req.slot_id}?action=save", json={"filename": filename})
                saved.raise_for_status()
            meta["snapshot_filename"] = filename
            try:
                snapshot_result = saved.json()
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(502, "llama.cpp returned an invalid slot save response") from exc
            if not isinstance(snapshot_result, dict):
                raise HTTPException(502, "llama.cpp returned an invalid slot save response")
            meta["snapshot_result"] = snapshot_result
            try:
                saved_count = int((meta["snapshot_result"] or {}).get("n_saved") or 0)
            except (TypeError, ValueError) as exc:
                raise HTTPException(502, "llama.cpp returned an invalid saved token count") from exc
            if saved_count != meta["token_count"]:
                raise HTTPException(502, "llama.cpp saved an incomplete prefix snapshot")
            snapshot = slot_snapshot_file(filename)
            if snapshot is None or not snapshot.is_file() or snapshot.stat().st_size <= 0:
                raise HTTPException(502, "prefix snapshot file is missing or empty")
            meta["snapshot_size"] = snapshot.stat().st_size
            meta["status"] = "ready"
            loaded_slot_prefixes[req.slot_id] = prefix_id
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 501:
                meta["status"] = "memory_ready"
                meta["warning"] = "llama.cpp was not started with --slot-save-path; cache is available only in the current slot until restart"
            else:
                meta["status"] = "prefill_failed"
                atomic_json(prefix_path(prefix_id), meta)
                raise HTTPException(502, "failed to prefill/save llama.cpp slot") from exc
        except httpx.HTTPError as exc:
            meta["status"] = "prefill_failed"
            atomic_json(prefix_path(prefix_id), meta)
            raise HTTPException(502, "failed to connect to llama.cpp") from exc
    atomic_json(prefix_path(prefix_id), meta)
    return meta


@app.get("/prefixes")
async def list_prefixes() -> list[dict[str, Any]]:
    return [cache_info(read_json(p)) for p in sorted((DATA_DIR / "prefixes").glob("*.json"))]


@app.get("/prefixes/{prefix_id}")
async def get_prefix(prefix_id: str) -> dict[str, Any]:
    return read_json(prefix_path(prefix_id))


@app.post("/prefixes/{prefix_id}/restore")
async def restore_prefix(prefix_id: str, req: PrefixRestore) -> dict[str, Any]:
    meta = read_json(prefix_path(prefix_id))
    if meta.get("status") == "memory_ready":
        if req.slot_id != meta.get("slot_id"):
            raise HTTPException(409, "memory-only prefix can be reused only in its original slot")
        return {"id": prefix_id, "status": "restored", "slot_id": req.slot_id, "storage": "memory"}
    if meta.get("status") != "ready" or not meta.get("snapshot_filename"):
        raise HTTPException(409, "prefix snapshot is not ready")
    async with prefix_request_lock:
        response = await llama(
            "POST",
            f"/slots/{req.slot_id}?action=restore",
            json={"filename": meta["snapshot_filename"]},
        )
        loaded_slot_prefixes[req.slot_id] = prefix_id
    runtime_event("info", "prefix_cache_restored_manual", prefix_id=prefix_id, slot_id=req.slot_id, restore_result=response.json())
    return {"id": prefix_id, "status": "restored", "slot_id": req.slot_id, "upstream": response.json()}


@app.delete("/prefixes/{prefix_id}")
async def delete_prefix(prefix_id: str) -> dict[str, str]:
    meta = read_json(prefix_path(prefix_id))
    prefix_path(prefix_id).unlink(missing_ok=True)
    template = template_path(prefix_id)
    template.unlink(missing_ok=True)
    snapshot = slot_snapshot_file(meta.get("snapshot_filename"))
    if snapshot:
        snapshot.unlink(missing_ok=True)
    for slot_id, bound_prefix_id in list(loaded_slot_prefixes.items()):
        if bound_prefix_id == prefix_id:
            loaded_slot_prefixes.pop(slot_id, None)
    runtime_event("info", "cache_deleted", prefix_id=prefix_id, snapshot=str(snapshot) if snapshot else None, template=str(template))
    return {"id": prefix_id, "status": "deleted"}


@app.post("/migrations")
async def migrate(req: MigrationRequest) -> dict[str, Any]:
    target = read_json(prefix_path(req.target_prefix_id))
    if req.mode == "none":
        state = {"session_id": req.session_id, "prefix_id": req.source_prefix_id, "status": "skipped", "updated_at": time.time()}
        atomic_json(session_path(req.session_id), state)
        return state
    if req.slot_id is None:
        raise HTTPException(400, "slot_id is required for full migration")
    # Restore a compatible prefix snapshot when registered; otherwise the caller can
    # still use the endpoint with a running llama.cpp slot and perform full prefill.
    async with prefix_request_lock:
        if target.get("status") == "ready" and target.get("snapshot_filename"):
            snapshot = slot_snapshot_file(target.get("snapshot_filename"))
            if snapshot is None or not snapshot.is_file() or snapshot.stat().st_size <= 0:
                raise HTTPException(409, "registered prefix snapshot is missing")
            await restore_slot_prefix(target, req.slot_id)
        await llama("POST", "/completion", json={"prompt": req.history, "cache_prompt": True, "id_slot": req.slot_id, "n_predict": 0})
        # The migration owns the target slot for the rest of this process. Keep
        # the local binding in sync so the next request does not restore the old
        # prefix again or claim a false in-memory reuse.
        loaded_slot_prefixes[req.slot_id] = req.target_prefix_id
    state = {"session_id": req.session_id, "prefix_id": req.target_prefix_id, "status": "succeeded", "mode": req.mode, "updated_at": time.time()}
    atomic_json(session_path(req.session_id), state)
    return state


@app.get("/migrations/{session_id}")
async def migration_status(session_id: str) -> dict[str, Any]:
    return read_json(session_path(session_id))


@app.post("/policy")
async def set_policy(req: SnapshotPolicy) -> dict[str, Any]:
    path = DATA_DIR / "policy.json"
    atomic_json(path, req.model_dump())
    return req.model_dump()


@app.get("/v1")
async def openai_api_info() -> dict[str, Any]:
    """Small discovery document for OpenAI-compatible Agent clients."""
    return {
        "object": "api",
        "provider": "llama-kv-middleware",
        "api_base": "/v1",
        "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/completions", "/v1/embeddings"],
    }


_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}

_INTERNAL_REQUEST_HEADERS = {
    "x-agent-id", "x-kv-agent-id", "x-session-id", "x-conversation-id",
    "x-kv-prefix-id", "x-kv-slot-id",
}


def upstream_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    dependencies=[Depends(serialize_prefix_requests)],
)
async def proxy_to_llama(path: str, request: Request) -> Response:
    """Transparent HTTP bridge for llama.cpp and its OpenAI-compatible API."""
    global route_requests, route_errors
    route_requests += 1
    upstream_path = "/" + path
    query = list(request.query_params.multi_items())
    body = await request.body()
    request_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "connection"} | _INTERNAL_REQUEST_HEADERS
    }
    if not middleware_enabled:
        request_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length", "connection"} | _INTERNAL_REQUEST_HEADERS
        }
        client = httpx.AsyncClient(base_url=LLAMA_BASE_URL, timeout=LLAMA_TIMEOUT)
        try:
            upstream = await client.send(client.build_request(request.method, upstream_path, params=query, headers=request_headers, content=body), stream=True)
            if upstream.status_code >= 400:
                route_errors += 1
            content_type = upstream.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                data = await upstream.aread()
                result = Response(data, status_code=upstream.status_code, headers=upstream_headers(upstream.headers), media_type=content_type or None)
                await upstream.aclose(); await client.aclose()
                return result
            async def passthrough():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose(); await client.aclose()
            return StreamingResponse(passthrough(), status_code=upstream.status_code, headers=upstream_headers(upstream.headers), media_type="text/event-stream")
        except httpx.HTTPError as exc:
            route_errors += 1
            await client.aclose()
            raise HTTPException(502, f"llama.cpp request failed: {exc}") from exc
    prefix_id = request.headers.get("x-kv-prefix-id")
    prefix_meta: dict[str, Any] | None = None
    slot_header = request.headers.get("x-kv-slot-id")
    agent_id = request.headers.get("x-agent-id") or request.headers.get("x-kv-agent-id")
    session_id = request.headers.get("x-session-id") or request.headers.get("x-conversation-id")
    explicit_session_id = session_id is not None
    request_payload: dict[str, Any] | None = None
    if upstream_path in {"/v1/chat/completions", "/v1/completions"}:
        try:
            parsed_payload = json.loads(body or b"{}")
            request_payload = parsed_payload if isinstance(parsed_payload, dict) else None
        except json.JSONDecodeError:
            request_payload = None
    # The model can be restarted independently from this middleware process.
    # Reconcile the process-local slot map before deciding whether a restore is
    # needed, otherwise the first request after a model restart skips the disk KV.
    if request_payload is not None and upstream_path in {"/v1/chat/completions", "/v1/completions"}:
        await detect_upstream_slot_reset()
    if agent_id is None and request_payload is not None:
        agent_id = request_payload.get("user")
    if agent_id is None:
        agent_id = f"agent-{uuid.uuid4().hex[:12]}"
    if session_id is None:
        session_id = f"session-{agent_id}"
    message_record: dict[str, Any] | None = None
    if upstream_path in {"/v1/chat/completions", "/v1/completions"}:
        try:
            message_record = {
                "time": time.time(),
                "agent_id": agent_id,
                "session_id": session_id,
                "user_question": extract_user_question(request_payload),
                "path": upstream_path,
                "request": request_payload or {},
                "response": None,
            }
            runtime_messages.append(message_record)
            runtime_event("info", "agent_request", agent_id=agent_id, session_id=session_id, user_question=message_record["user_question"], path=upstream_path)
        except (TypeError, ValueError):
            pass
    if prefix_id and upstream_path in {"/v1/chat/completions", "/v1/completions", "/completion"}:
        if slot_header is None or not slot_header.isdigit():
            raise HTTPException(400, "X-KV-Slot-ID is required with X-KV-Prefix-ID")
        slot_id = int(slot_header)
        meta = read_json(prefix_path(prefix_id))
        prefix_meta = meta
        if message_record is not None:
            message_record["prefix_id"] = prefix_id
            message_record["slot_id"] = slot_id
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        if upstream_path == "/v1/chat/completions":
            if meta.get("prefix_hash"):
                _, incoming_hash = agent_prefix(payload)
                if incoming_hash != meta["prefix_hash"]:
                    raise HTTPException(409, "Agent template does not match the cached prefix")
            else:
                messages = payload.get("messages") or []
                system_text = messages[0].get("content") if messages and messages[0].get("role") == "system" else None
                if system_text != meta.get("text"):
                    raise HTTPException(409, "OpenAI system message does not exactly match the cached prefix")
            payload = sanitize_payload_prefix(payload)
        else:
            prompt = payload.get("prompt", "")
            if isinstance(prompt, str) and not prompt.startswith(meta["text"]):
                raise HTTPException(409, "prompt does not start with the cached prefix")
        if meta.get("status") == "memory_ready" and slot_id != meta.get("slot_id"):
            raise HTTPException(409, "memory-only prefix must use its original slot")
        if meta.get("status") == "ready":
            if not snapshot_is_prefix_only(meta):
                raise HTTPException(409, "prefix snapshot is not a prefix-only snapshot")
            restored = False
            restore_result: dict[str, Any] | None = None
            if loaded_slot_prefixes.get(slot_id) != prefix_id:
                restore_result = await restore_slot_prefix(meta, slot_id)
                loaded_slot_prefixes[slot_id] = prefix_id
                restored = True
                if message_record is not None:
                    # Restore is only a preparation step. The final hit/miss must
                    # come from llama.cpp's usage/timings in the response.
                    message_record["cache_hit"] = None
            elif message_record is not None:
                message_record["cache_hit"] = True
            runtime_event(
                "info",
                "prefix_cache_restored" if restored else "prefix_cache_reused_in_memory",
                prefix_id=prefix_id,
                slot_id=slot_id,
                filename=meta["snapshot_filename"],
                restored=restored,
                restore_result=restore_result,
            )
        elif meta.get("status") != "memory_ready":
            raise HTTPException(409, "prefix cache is not ready")
        else:
            loaded_slot_prefixes[slot_id] = prefix_id
            if message_record is not None:
                message_record["cache_hit"] = True
                message_record["prefix_id"] = prefix_id
                message_record["slot_id"] = slot_id
                message_record["cached_tokens"] = int(meta.get("token_count") or 0)
        payload["id_slot"] = slot_id
        payload["cache_prompt"] = True
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    elif upstream_path == "/v1/chat/completions":
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "request body must be a JSON object")
        prefix_value, prefix_hash = agent_prefix(payload)
        if prefix_value["messages"] or prefix_value["tools"]:
            resolved_agent_id = agent_id or payload.get("user") or f"auto-{prefix_hash[:16]}"
            if not request.headers.get("x-agent-id") and not request.headers.get("x-kv-agent-id") and not payload.get("user"):
                resolved_agent_id = f"auto-{prefix_hash[:16]}"
            agent_id = resolved_agent_id
            if not explicit_session_id:
                session_id = f"session-{resolved_agent_id}"
            if message_record is not None:
                message_record["agent_id"] = agent_id
                message_record["session_id"] = session_id
            prefix = await resolve_agent_prefix(resolved_agent_id, payload, int(slot_header) if slot_header and slot_header.isdigit() else None)
            prefix_status = prefix.get("status")
            try:
                prefix_slot_id = int(prefix.get("slot_id"))
            except (TypeError, ValueError):
                prefix_slot_id = None
            ready_snapshot = slot_snapshot_file(prefix.get("snapshot_filename")) if prefix_status == "ready" else None
            ready_snapshot_missing = (
                prefix_status == "ready"
                and (ready_snapshot is None or not ready_snapshot.is_file() or ready_snapshot.stat().st_size <= 0)
            )
            memory_prefix_needs_build = (
                prefix_status == "memory_ready"
                and prefix_slot_id is not None
                and loaded_slot_prefixes.get(prefix_slot_id) != prefix.get("id")
            )
            needs_prefix_build = (
                prefix_status in {"building", "failed"}
                or memory_prefix_needs_build
                or (prefix_status == "ready" and (not snapshot_is_prefix_only(prefix) or ready_snapshot_missing))
                or prefix_status not in {"building", "failed", "memory_ready", "ready"}
            )
            if needs_prefix_build:
                prefix.setdefault("snapshot_filename", f"prefix-{prefix['id']}.bin")
                prefix["status"] = "building"
                atomic_json(prefix_path(prefix["id"]), prefix)
                # Save a slot containing only the reusable template before the
                # request adds its per-window user message.
                try:
                    await build_and_save_prefix(prefix, payload)
                    prefix = read_json(prefix_path(prefix["id"]))
                    if prefix.get("status") in {"ready", "memory_ready"}:
                        loaded_slot_prefixes[int(prefix["slot_id"])] = prefix["id"]
                        runtime_event("info", "prefix_cache_built", prefix_id=prefix["id"], slot_id=prefix["slot_id"], token_count=prefix.get("token_count"), status=prefix.get("status"))
                    else:
                        runtime_event("error", "prefix_cache_build_incomplete", prefix_id=prefix["id"], slot_id=prefix.get("slot_id"), status=prefix.get("status"), error=prefix.get("save_error"))
                except HTTPException as exc:
                    prefix["status"] = "failed"
                    prefix["save_error"] = str(exc.detail)
                    atomic_json(prefix_path(prefix["id"]), prefix)
                    save_after_response = True
                    runtime_event("error", "prefix_build_failed_fallback", prefix_id=prefix["id"], error=str(exc.detail))
            elif prefix.get("status") == "ready":
                snapshot = slot_snapshot_file(prefix.get("snapshot_filename"))
                if snapshot is None or not snapshot.is_file() or snapshot.stat().st_size <= 0:
                    # The normal path rebuilds a missing snapshot before the
                    # user request. Keep this guard for records changed by an
                    # administrator between the checks above.
                    prefix["status"] = "failed"
                    prefix["save_error"] = "snapshot disappeared before prefix rebuild"
                    atomic_json(prefix_path(prefix["id"]), prefix)
                else:
                    try:
                        slot_id = int(prefix["slot_id"])
                        restored = False
                        restore_result: dict[str, Any] | None = None
                        if loaded_slot_prefixes.get(slot_id) != prefix["id"]:
                            restore_result = await restore_slot_prefix(prefix, slot_id)
                            loaded_slot_prefixes[slot_id] = prefix["id"]
                            restored = True
                            if message_record is not None:
                                # Restore is only a preparation step. The final
                                # hit/miss must come from llama.cpp's response.
                                message_record["cache_hit"] = None
                        elif message_record is not None:
                            message_record["cache_hit"] = True
                        runtime_event(
                            "info",
                            "prefix_cache_restored" if restored else "prefix_cache_reused_in_memory",
                            prefix_id=prefix["id"],
                            slot_id=prefix["slot_id"],
                            filename=prefix["snapshot_filename"],
                            restored=restored,
                            restore_result=restore_result,
                        )
                    except HTTPException as exc:
                        runtime_event("error", "prefix_restore_failed_fallback", prefix_id=prefix["id"], error=str(exc.detail))
                        prefix["status"] = "failed"
                        prefix["save_error"] = str(exc.detail)
                        atomic_json(prefix_path(prefix["id"]), prefix)
            elif prefix.get("status") == "memory_ready":
                # Persistent snapshots are unavailable, but the prefix is still
                # valid in the currently bound slot until llama.cpp restarts.
                slot_id = int(prefix["slot_id"])
                loaded_slot_prefixes[slot_id] = prefix["id"]
                if message_record is not None:
                    message_record["cache_hit"] = True
                runtime_event(
                    "info",
                    "prefix_cache_reused_in_memory",
                    prefix_id=prefix["id"],
                    slot_id=slot_id,
                    filename=prefix.get("snapshot_filename"),
                    restored=False,
                    restore_result=None,
                )
            else:
                # A stale record should not block the Agent. Mark it for a clean
                # prefix rebuild on the current request; never save after the
                # request because that slot may contain user/session tokens.
                prefix["status"] = "building"
                atomic_json(prefix_path(prefix["id"]), prefix)
            prefix_id = prefix["id"]
            prefix_meta = prefix
            if message_record is not None:
                message_record["prefix_id"] = prefix_id
                message_record["slot_id"] = prefix["slot_id"]
                message_record.setdefault("cache_hit", None)
                message_record.setdefault("cached_tokens", None)
            if prefix.get("status") in {"building", "failed"}:
                runtime_event("info", "prefix_cache_building", prefix_id=prefix_id, slot_id=prefix["slot_id"])
            payload = sanitize_payload_prefix(payload)
            payload["id_slot"] = prefix["slot_id"]
            payload["cache_prompt"] = True
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    client = httpx.AsyncClient(base_url=LLAMA_BASE_URL, timeout=LLAMA_TIMEOUT)
    try:
        if request.method == "OPTIONS":
            response = await client.request(request.method, upstream_path, params=query, headers=request_headers, content=body)
            data = response.content
            result = Response(data, status_code=response.status_code, headers=upstream_headers(response.headers), media_type=response.headers.get("content-type"))
            await client.aclose()
            return result

        upstream = await client.send(
            client.build_request(request.method, upstream_path, params=query, headers=request_headers, content=body),
            stream=True,
        )
        if upstream.status_code >= 400:
            data = await upstream.aread()
            # A normal Agent request can fail after a valid prefix was restored
            # (for example because its total context exceeds the slot limit).
            # Do not invalidate the separately persisted prefix snapshot.
            if prefix_meta is not None and prefix_meta.get("status") != "ready":
                prefix_meta["status"] = "failed"
                prefix_meta["save_error"] = f"upstream returned HTTP {upstream.status_code} before slot save"
                atomic_json(prefix_path(prefix_meta["id"]), prefix_meta)
                runtime_event("error", "prefix_build_upstream_error", prefix_id=prefix_meta["id"], status_code=upstream.status_code)
            result = Response(data, status_code=upstream.status_code, headers=upstream_headers(upstream.headers), media_type=upstream.headers.get("content-type"))
            await upstream.aclose()
            await client.aclose()
            return result

        content_type = upstream.headers.get("content-type", "")
        response_headers = upstream_headers(upstream.headers)
        if prefix_id:
            response_headers["x-kv-prefix-id"] = prefix_id
        is_stream = "text/event-stream" in content_type or request.headers.get("accept", "").startswith("text/event-stream")
        if not is_stream:
            data = await upstream.aread()
            if message_record is not None:
                try:
                    message_record["response"] = json.loads(data)
                    stats = response_cache_stats(message_record["response"])
                    merge_cache_stats(message_record, stats)
                    if stats.get("cached_tokens") is not None:
                        clear_binding_after_cache_miss(prefix_meta, message_record)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    message_record["response"] = data[:262144].decode("utf-8", errors="replace")
                message_record["status_code"] = upstream.status_code
                runtime_event("info", "prefix_cache_result", prefix_id=prefix_id, slot_id=(prefix_meta or {}).get("slot_id"), cache_hit=message_record.get("cache_hit") if message_record.get("cache_hit") is not None else "unknown", cached_tokens=message_record.get("cached_tokens"), prompt_tokens=message_record.get("prompt_tokens"), stats_available=stats.get("cached_tokens") is not None)
            result = Response(data, status_code=upstream.status_code, headers=response_headers, media_type=content_type or None)
            await upstream.aclose()
            await client.aclose()
            return result

        async def stream_body():
            captured = bytearray()
            tail = bytearray()
            total = 0
            try:
                async for chunk in upstream.aiter_raw():
                    if len(captured) < 262144:
                        captured.extend(chunk[:262144 - len(captured)])
                    total += len(chunk)
                    tail.extend(chunk)
                    if len(tail) > 65536:
                        del tail[:len(tail) - 65536]
                    yield chunk
            finally:
                if message_record is not None:
                    message_record["response"] = captured.decode("utf-8", errors="replace")
                    stats = response_text_cache_stats(message_record["response"])
                    if total > len(captured):
                        # llama.cpp sends usage in the final SSE chunk. A stream
                        # longer than the capture window would otherwise drop the
                        # authoritative cache statistics; parse a rolling tail too.
                        for key, value in response_text_cache_stats(tail.decode("utf-8", errors="replace")).items():
                            if value is not None:
                                stats[key] = value
                    merge_cache_stats(message_record, stats)
                    if stats.get("cached_tokens") is not None:
                        clear_binding_after_cache_miss(prefix_meta, message_record)
                    message_record["status_code"] = upstream.status_code
                    message_record["stream"] = True
                    runtime_event("info", "prefix_cache_result", prefix_id=prefix_id, slot_id=(prefix_meta or {}).get("slot_id"), cache_hit=message_record.get("cache_hit") if message_record.get("cache_hit") is not None else "unknown", cached_tokens=message_record.get("cached_tokens"), prompt_tokens=message_record.get("prompt_tokens"), stats_available=stats.get("cached_tokens") is not None, stream=True)
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(stream_body(), status_code=upstream.status_code, headers=response_headers, media_type="text/event-stream")
    except httpx.HTTPError as exc:
        await client.aclose()
        runtime_event("error", "upstream_request_failed", path=upstream_path, error=str(exc))
        raise HTTPException(502, f"llama.cpp request failed: {exc}") from exc
