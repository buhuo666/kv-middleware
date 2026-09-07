from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="module")
def app_module(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("runtime")
    config = root / "config.yml"
    config.write_text(
        """
llama_cpp:
  base_url: http://llama-cpp.invalid:8080
  timeout_seconds: 10
  slot_save_path: ./slot-cache
agent:
  host: 127.0.0.1
  port: 18000
storage:
  data_dir: ./data
""".strip(),
        encoding="utf-8",
    )
    old_config = os.environ.get("KV_CONFIG")
    os.environ["KV_CONFIG"] = str(config)
    project_root = str(Path(__file__).resolve().parents[1])
    sys.path.insert(0, project_root)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    yield module
    sys.modules.pop("app", None)
    if sys.path and sys.path[0] == project_root:
        sys.path.pop(0)
    if old_config is None:
        os.environ.pop("KV_CONFIG", None)
    else:
        os.environ["KV_CONFIG"] = old_config


def test_agent_prefix_excludes_user_messages(app_module):
    payload = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "developer", "content": "rules"},
            {"role": "user", "content": "not cached"},
        ],
        "tools": [{"type": "function", "function": {"name": "ping"}}],
    }
    prefix, fingerprint = app_module.agent_prefix(payload)
    assert [message["role"] for message in prefix["messages"]] == ["system", "developer"]
    assert prefix["tools"] == payload["tools"]
    assert len(fingerprint) == 64


def test_response_cache_stats_supports_openai_and_llama_timings(app_module):
    assert app_module.response_cache_stats({
        "usage": {"prompt_tokens_details": {"cached_tokens": 12}, "prompt_tokens": 20},
    }) == {"prompt_tokens": 20, "cached_tokens": 12, "cache_hit": True}
    assert app_module.response_cache_stats({
        "timings": {"prompt_n": 8, "cache_n": 12},
    }) == {"prompt_tokens": None, "cached_tokens": 12, "cache_hit": True}
    # ``prompt_n`` is the evaluated portion, not proof of a cache hit.
    assert app_module.response_cache_stats({
        "timings": {"prompt_n": 20},
    }) == {"prompt_tokens": None, "cached_tokens": None}


def test_agent_prefix_ignores_malformed_leading_message(app_module):
    prefix, _ = app_module.agent_prefix({"messages": ["not-a-message"]})
    assert prefix == {"messages": [], "tools": []}


def test_repair_does_not_promote_unverified_snapshot(app_module):
    prefix_id = "repair-test"
    snapshot_root = Path(app_module.CONFIG["llama_cpp"]["slot_save_path"])
    if not snapshot_root.is_absolute():
        snapshot_root = app_module.BASE_DIR / snapshot_root
    snapshot_root.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_root / f"prefix-{prefix_id}.bin"
    snapshot.write_bytes(b"stale")
    metadata = {
        "id": prefix_id,
        "status": "building",
        "snapshot_filename": snapshot.name,
        "created_at": 0,
        "token_count": 2,
        "snapshot_scope": "prefix_only_v2",
        "snapshot_result": {"n_saved": 3},
    }
    app_module.atomic_json(app_module.prefix_path(prefix_id), metadata)
    app_module.repair_cache_states()
    assert app_module.read_json(app_module.prefix_path(prefix_id))["status"] == "failed"


def test_save_agent_slot_rejects_unverified_snapshot(app_module, monkeypatch):
    prefix_id = "unverified-save-test"
    app_module.atomic_json(
        app_module.prefix_path(prefix_id),
        {"id": prefix_id, "status": "building", "slot_id": 0, "snapshot_filename": f"prefix-{prefix_id}.bin"},
    )
    snapshot_root = Path(app_module.CONFIG["llama_cpp"]["slot_save_path"])
    if not snapshot_root.is_absolute():
        snapshot_root = app_module.BASE_DIR / snapshot_root
    snapshot_root.mkdir(parents=True, exist_ok=True)
    observed = {}

    async def fake_llama(method, path, **kwargs):
        observed["request"] = (method, path)
        (snapshot_root / f"prefix-{prefix_id}.bin").write_bytes(b"full")
        return httpx.Response(200, json={"n_saved": 1})

    monkeypatch.setattr(app_module, "llama", fake_llama)
    async def run_save():
        await app_module.save_agent_slot({
            "id": prefix_id,
            "slot_id": 0,
            "snapshot_filename": f"prefix-{prefix_id}.bin",
            "snapshot_scope": "full_conversation",
            "token_count": 1,
        })

    asyncio.run(run_save())
    assert observed["request"] == ("POST", "/slots/0?action=save")
    assert app_module.read_json(app_module.prefix_path(prefix_id))["status"] == "failed"


def test_snapshot_path_stays_inside_configured_root(app_module):
    snapshot = app_module.slot_snapshot_file("prefix-test.bin")
    assert snapshot is not None
    assert snapshot.name == "prefix-test.bin"
    assert snapshot.parent.name == "slot-cache"
    with pytest.raises(app_module.HTTPException) as exc:
        app_module.slot_snapshot_file("../outside.bin")
    assert exc.value.status_code == 400


def test_prefix_id_rejects_path_traversal(app_module):
    with pytest.raises(app_module.HTTPException) as exc:
        app_module.prefix_path("../outside")
    assert exc.value.status_code == 400


def test_delete_prefix_removes_files_and_binding(app_module):
    prefix_id = "delete-test"
    snapshot_root = Path(app_module.CONFIG["llama_cpp"]["slot_save_path"])
    if not snapshot_root.is_absolute():
        snapshot_root = app_module.BASE_DIR / snapshot_root
    snapshot_root.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_root / f"prefix-{prefix_id}.bin"
    snapshot.write_bytes(b"kv")
    app_module.atomic_json(
        app_module.prefix_path(prefix_id),
        {"id": prefix_id, "snapshot_filename": snapshot.name},
    )
    app_module.template_path(prefix_id).write_text("{}", encoding="utf-8")
    app_module.loaded_slot_prefixes[0] = prefix_id

    result = asyncio.run(app_module.delete_prefix(prefix_id))

    assert result["status"] == "deleted"
    assert not snapshot.exists()
    assert not app_module.prefix_path(prefix_id).exists()
    assert not app_module.template_path(prefix_id).exists()
    assert 0 not in app_module.loaded_slot_prefixes


def test_migration_restores_snapshot_and_uses_id_slot(app_module, monkeypatch):
    prefix_id = "migration-test"
    snapshot_root = Path(app_module.CONFIG["llama_cpp"]["slot_save_path"])
    if not snapshot_root.is_absolute():
        snapshot_root = app_module.BASE_DIR / snapshot_root
    snapshot_root.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_root / f"prefix-{prefix_id}.bin"
    snapshot.write_bytes(b"kv")
    app_module.atomic_json(
        app_module.prefix_path(prefix_id),
        {
            "id": prefix_id,
            "status": "ready",
            "snapshot_filename": snapshot.name,
            "token_count": 2,
        },
    )
    observed: dict[str, object] = {}

    async def fake_restore(meta, slot_id):
        observed["restored"] = (meta["id"], slot_id)
        return {"n_restored": 2}

    async def fake_llama(method, path, **kwargs):
        observed["completion"] = (method, path, kwargs.get("json"))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(app_module, "restore_slot_prefix", fake_restore)
    monkeypatch.setattr(app_module, "llama", fake_llama)
    app_module.prefix_request_lock = asyncio.Lock()
    request = app_module.MigrationRequest(
        session_id="session-test",
        target_prefix_id=prefix_id,
        mode="full",
        history="history",
        slot_id=0,
    )

    result = asyncio.run(app_module.migrate(request))

    assert result["status"] == "succeeded"
    assert observed["restored"] == (prefix_id, 0)
    assert app_module.loaded_slot_prefixes[0] == prefix_id
    payload = observed["completion"][2]
    assert payload["id_slot"] == 0
    assert "slot_id" not in payload


def test_invalid_config_has_clear_error(app_module, tmp_path, monkeypatch):
    invalid = tmp_path / "invalid.yml"
    invalid.write_text("agent: []", encoding="utf-8")
    monkeypatch.setattr(app_module, "CONFIG_PATH", invalid)
    with pytest.raises(RuntimeError, match="agent.*mapping"):
        app_module.load_config()
