"""Per-key catalogs must remain isolated through every metadata cache layer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from agent import model_metadata as metadata
from agent.context_compressor import ContextCompressor


def test_remote_disk_catalog_is_scoped_to_credential(tmp_path, monkeypatch):
    """A cold worker must not inherit another key's smaller context window."""
    import httpx

    model = "tenant/gpt-6.1-sol"
    endpoint = "https://catalog.example.invalid/v1"
    contexts = {"limited-key": 256_000, "extended-key": 872_000}
    requests = []

    def respond(request):
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        requests.append(key)
        return httpx.Response(200, json={"data": [{"id": model, "context_length": contexts[key]}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache_time", {})
    monkeypatch.setattr(metadata, "_get_endpoint_metadata_cache_path", lambda: tmp_path / "catalog.json")

    assert metadata.fetch_endpoint_model_metadata(endpoint, api_key="limited-key")[model]["context_length"] == contexts["limited-key"]
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    compressor = ContextCompressor(model=model, provider="test", base_url=endpoint, api_key="extended-key", quiet_mode=True)
    assert compressor.context_length == contexts["extended-key"]
    assert requests == ["limited-key", "extended-key"]

    # Same credential still gets the cross-process memo without another request.
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    assert metadata.fetch_endpoint_model_metadata(endpoint, api_key="limited-key")[model]["context_length"] == contexts["limited-key"]
    assert metadata.fetch_endpoint_model_metadata(endpoint, api_key="extended-key")[model]["context_length"] == contexts["extended-key"]
    assert requests == ["limited-key", "extended-key"]
    persisted = (tmp_path / "catalog.json").read_text(encoding="utf-8")
    assert "limited-key" not in persisted and "extended-key" not in persisted


def test_custom_context_catalog_beats_legacy_unscoped_scalar(tmp_path, monkeypatch):
    """Restarted compressors must resolve the current credential, not an eternal URL-only value."""
    import httpx

    model = "tenant/gpt-6.1-sol"
    endpoint = "https://catalog.example.invalid/v1"
    contexts = {"limited-key": 256_000, "extended-key": 872_000}
    requests = []

    def respond(request):
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        requests.append(key)
        return httpx.Response(200, json={"data": [{"id": model, "context_length": contexts[key]}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache_time", {})
    monkeypatch.setattr(metadata, "_get_endpoint_metadata_cache_path", lambda: tmp_path / "catalog.json")
    monkeypatch.setattr(metadata, "_get_context_cache_path", lambda: tmp_path / "contexts.yaml")
    metadata.save_context_length(model, endpoint, contexts["limited-key"])

    for key in ("extended-key", "limited-key", "extended-key"):
        compressor = ContextCompressor(model=model, provider="test", base_url=endpoint, api_key=key, quiet_mode=True)
        assert compressor.context_length == contexts[key]
    assert requests == ["extended-key", "limited-key"]

    # Policy changes at the same credential must be seen after explicit metadata refresh.
    contexts["extended-key"] = contexts["limited-key"]
    metadata.fetch_endpoint_model_metadata(endpoint, api_key="extended-key", force_refresh=True)
    compressor = ContextCompressor(model=model, provider="test", base_url=endpoint, api_key="extended-key", quiet_mode=True)
    assert compressor.context_length == contexts["extended-key"]
    before_pin = list(requests)
    assert metadata.get_model_context_length(model, endpoint, api_key="extended-key", provider="test", config_context_length=123_456) == 123_456
    assert requests == before_pin


@pytest.fixture
def endpoint_catalog(tmp_path, monkeypatch):
    """Use real probe/cache code with transport-only network substitution."""
    path = tmp_path / "catalog.json"
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache_time", {})
    monkeypatch.setattr(metadata, "_endpoint_blackhole_cache", {})
    monkeypatch.setattr(metadata, "_get_endpoint_metadata_cache_path", lambda: path)
    real_client = httpx.Client

    def install(respond):
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
            **{**kwargs, "transport": httpx.MockTransport(respond)},
        ))
        return path

    return install


@pytest.mark.parametrize("refresh_status", [401, 403, 200])
@pytest.mark.parametrize("sibling_stamp", [
    pytest.param(None, id="no-corrupt-sibling"),
    pytest.param("invalid", id="string"),
    pytest.param(10**400, id="oversized-positive-int"),
    pytest.param(-(10**400), id="oversized-negative-int"),
    pytest.param(float("nan"), id="numeric-nan"),
    pytest.param(float("inf"), id="numeric-positive-infinity"),
    pytest.param(float("-inf"), id="numeric-negative-infinity"),
])
def test_authoritative_negative_refresh_replaces_disk_positive(
    endpoint_catalog, tmp_path, refresh_status, sibling_stamp,
):
    """Denial or an empty catalog must survive a cold cache without harming a sibling key."""
    model = "tenant/entitled-model"
    endpoint = "https://catalog.example.invalid/v1"
    requests = []
    revoked = False

    def respond(request):
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        requests.append(key)
        if revoked and key == "revoked-key":
            return httpx.Response(refresh_status, json={"data": []})
        return httpx.Response(200, json={"data": [{
            "id": model, "context_length": 872_000, "service_tiers": ["priority"],
        }]})

    path = endpoint_catalog(respond)
    positive = metadata.fetch_endpoint_model_metadata(endpoint, api_key="revoked-key")
    sibling = metadata.fetch_endpoint_model_metadata(endpoint, api_key="sibling-key")
    assert positive[model]["context_length"] == 872_000
    if sibling_stamp is not None:
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows["unrelated-corrupt-entry"] = {"at": sibling_stamp, "models": {}}
        path.write_text(json.dumps(rows), encoding="utf-8")
    revoked = True
    assert metadata.fetch_endpoint_model_metadata(endpoint, api_key="revoked-key", force_refresh=True) == {}
    assert requests == ["revoked-key", "sibling-key", "revoked-key"]

    # A new interpreter, not cleared module dictionaries, must observe the revocation.
    cold_home = tmp_path / "cold-home"
    cold_home.mkdir()
    child_env = {name: os.environ[name] for name in (
        "SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "PATHEXT", "ComSpec",
    ) if name in os.environ}
    child_env.update({
        "HOME": str(cold_home), "USERPROFILE": str(cold_home),
        "LOCALAPPDATA": str(cold_home), "APPDATA": str(cold_home),
        "HERMES_HOME": str(cold_home / "hermes"),
        "TEMP": str(cold_home), "TMP": str(cold_home), "TMPDIR": str(cold_home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_TEST_ISOLATION": "1",
        "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
    })
    child = subprocess.run([
        sys.executable, "-I", "-c", """
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

def no_network(*args, **kwargs):
    raise AssertionError("cold catalog regression prohibits live network")

socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
sys.path.insert(0, sys.argv[1])
import httpx
from agent import model_metadata as metadata
from hermes_cli.models_fast_custom import resolve_custom_priority_overrides

assert metadata._endpoint_model_metadata_cache == {}
assert metadata._endpoint_model_metadata_cache_time == {}
path, endpoint, model = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
metadata._get_endpoint_metadata_cache_path = lambda: path
requests = []
def no_probe(request):
    requests.append(str(request.url))
    raise AssertionError("fresh negative and sibling catalogs must be read from disk")

real_client = httpx.Client
httpx.Client = lambda **kwargs: real_client(
    **{**kwargs, "transport": httpx.MockTransport(no_probe)},
)
config = {"providers": {"catalog": {
    "base_url": endpoint, "capabilities": {"allow_paid_priority": True},
}}}
def priority(key):
    return resolve_custom_priority_overrides(
        model, provider="catalog", base_url=endpoint, api_key=key,
        api_mode="chat_completions", tier="priority",
    )

with patch("hermes_cli.config.load_config_readonly", return_value=config):
    result = {
        "pid": os.getpid(),
        "target_catalog": metadata.fetch_endpoint_model_metadata(endpoint, api_key="revoked-key"),
        "target_context": metadata._resolve_endpoint_context_length(model, endpoint, api_key="revoked-key"),
        "target_priority": priority("revoked-key"),
        "sibling_catalog": metadata.fetch_endpoint_model_metadata(endpoint, api_key="sibling-key"),
        "sibling_priority": priority("sibling-key"),
        "requests": requests,
    }
print(json.dumps(result))
""", str(Path(__file__).resolve().parents[2]), str(path), endpoint, model,
    ], cwd=cold_home, env=child_env, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stdout + child.stderr
    result = json.loads(child.stdout)
    assert result.pop("pid") != os.getpid()
    assert result == {
        "target_catalog": {}, "target_context": None, "target_priority": None,
        "sibling_catalog": sibling, "sibling_priority": {"service_tier": "priority"},
        "requests": [],
    }
    assert requests == ["revoked-key", "sibling-key", "revoked-key"]
    rows = json.loads(path.read_text(encoding="utf-8"))
    target = json.dumps(metadata._endpoint_memo_key(endpoint, "revoked-key"), separators=(",", ":"))
    other = json.dumps(metadata._endpoint_memo_key(endpoint, "sibling-key"), separators=(",", ":"))
    assert rows[target]["models"] == {}
    assert rows[other]["models"] == sibling
    assert "unrelated-corrupt-entry" not in rows


@pytest.mark.parametrize("cold_worker", [False, True])
@pytest.mark.parametrize("source_kind", ["callable", "command"])
def test_dynamic_credentials_never_share_catalog_without_credential_identity(
    endpoint_catalog, monkeypatch, cold_worker, source_kind,
):
    """Opaque sources (even one command) cannot prove the current principal across cache reads."""
    from agent import command_token_source

    model = "tenant/dynamic-model"
    endpoint = "https://catalog.example.invalid/v1"
    contexts = {"extended-key": 872_000, "limited-key": 256_000}
    requests = []
    minted = []

    def token(key):
        minted.append(key)
        return key

    if source_kind == "command":
        monkeypatch.setattr(command_token_source, "_mint", lambda command, label: (token(label), 3600))
        sources = {key: command_token_source.CommandTokenSource("same-sso-command", key) for key in contexts}
        assert sources["extended-key"].cache_identity == sources["limited-key"].cache_identity
    else:
        sources = {key: (lambda key=key: token(key)) for key in contexts}

    def respond(request):
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        requests.append(key)
        return httpx.Response(200, json={"data": [{"id": model, "context_length": contexts[key]}]})

    path = endpoint_catalog(respond)
    for key in ("extended-key", "limited-key", "extended-key"):
        if cold_worker:
            metadata._endpoint_model_metadata_cache.clear()
            metadata._endpoint_model_metadata_cache_time.clear()
        compressor = ContextCompressor(
            model=model, provider="test", base_url=endpoint, api_key=sources[key], quiet_mode=True,
        )
        assert compressor.context_length == contexts[key]
    assert requests == ["extended-key", "limited-key", "extended-key"]
    expected_mints = requests if source_kind == "callable" else requests[:2]
    assert minted == expected_mints  # No extra mint solely to compute a cache identity.
    assert not path.exists()
    assert metadata._endpoint_model_metadata_cache == {}
    assert metadata._endpoint_model_metadata_cache_time == {}


@pytest.mark.parametrize("expires_at", [300.0, 500.0])
def test_disk_hit_preserves_origin_ttl_for_context_and_priority(endpoint_catalog, monkeypatch, expires_at):
    """Loading a 299-second-old catalog must not grant another five-minute paid entitlement."""
    from types import SimpleNamespace

    from hermes_cli.models_fast_custom import resolve_custom_priority_overrides

    model = "tenant/ttl-model"
    endpoint = "https://catalog.example.invalid/v1"
    now = [0.0]
    requests = []
    monkeypatch.setattr(metadata, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"providers": {"catalog": {
        "base_url": endpoint, "capabilities": {"allow_paid_priority": True},
    }}})

    def respond(request):
        requests.append(request)
        extended = now[0] < metadata._ENDPOINT_MODEL_CACHE_TTL
        return httpx.Response(200, json={"data": [{
            "id": model, "context_length": 872_000 if extended else 256_000,
            "service_tiers": ["priority"] if extended else [],
        }]})

    path = endpoint_catalog(respond)

    def priority():
        return resolve_custom_priority_overrides(
            model, provider="catalog", base_url=endpoint, api_key="tenant-key",
            api_mode="chat_completions", tier="priority",
        )

    assert priority() == {"service_tier": "priority"}
    metadata._endpoint_model_metadata_cache.clear()
    metadata._endpoint_model_metadata_cache_time.clear()
    now[0] = 299.0
    assert priority() == {"service_tier": "priority"}
    assert len(requests) == 1
    assert next(iter(json.loads(path.read_text(encoding="utf-8")).values()))["at"] == 0.0
    now[0] = expires_at
    assert priority() is None
    compressor = ContextCompressor(
        model=model, provider="test", base_url=endpoint, api_key="tenant-key", quiet_mode=True,
    )
    assert compressor.context_length == 256_000
    assert len(requests) == 2


@pytest.mark.parametrize("layer", ["disk", "memory"])
@pytest.mark.parametrize("stamp", [
    None, True, "invalid", "NaN", "Infinity", 101.0,
    pytest.param(10**400, id="oversized-positive-int"),
    pytest.param(-(10**400), id="oversized-negative-int"),
    pytest.param(float("nan"), id="numeric-nan"),
    pytest.param(float("inf"), id="numeric-positive-infinity"),
    pytest.param(float("-inf"), id="numeric-negative-infinity"),
])
def test_missing_or_untrustworthy_fetch_time_cannot_authorize_catalog(endpoint_catalog, monkeypatch, layer, stamp):
    """Unknown, non-finite or future age is a cache miss, not renewed freshness."""
    from types import SimpleNamespace

    model = "tenant/stale-model"
    endpoint = "https://catalog.example.invalid/v1"
    key = "tenant-key"
    requests = []
    monkeypatch.setattr(metadata, "time", SimpleNamespace(time=lambda: 100.0))

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{
            "id": model, "context_length": 256_000, "service_tiers": [],
        }]})

    path = endpoint_catalog(respond)
    memo_key = metadata._endpoint_memo_key(endpoint, key)
    positive = {model: {"id": model, "context_length": 872_000, "service_tiers": ["priority"]}}
    if layer == "disk":
        row = {"models": positive}
        if stamp is not None:
            row["at"] = stamp
        path.write_text(json.dumps({json.dumps(memo_key, separators=(",", ":")): row}), encoding="utf-8")
    else:
        metadata._endpoint_model_metadata_cache[memo_key] = positive
        if stamp is not None:
            metadata._endpoint_model_metadata_cache_time[memo_key] = stamp
    refreshed = metadata.fetch_endpoint_model_metadata(endpoint, api_key=key)
    assert refreshed[model]["context_length"] == 256_000
    assert refreshed[model]["service_tiers"] == []
    assert len(requests) == 1
