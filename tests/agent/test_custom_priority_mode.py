"""Paid custom priority needs consent and the current credential's exact catalog row."""
from __future__ import annotations

import copy
import json
from collections import UserDict
from types import MappingProxyType, SimpleNamespace

import pytest
import httpx
from openai import OpenAI

from agent import fast_mode, model_metadata
from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_cli.models import resolve_fast_mode_overrides


def test_custom_priority_requires_consent_and_exact_credential_catalog(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    endpoint, model = "https://catalog.example.invalid/v1", "tenant/gpt-6.1-sol"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config = {"providers": {"test": {"api": endpoint, "capabilities": {"allow_paid_priority": False}}}}
    atomic_config_write(config_path, config)
    seen = []
    catalogs = {"extended-key": ["priority"], "limited-key": []}

    def respond(request):
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        seen.append(key)
        return httpx.Response(200, json={"data": [{"id": model, "service_tiers": catalogs[key]}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache_time", {})
    kwargs = {"provider": "test", "base_url": endpoint, "api_key": "extended-key", "api_mode": "chat_completions"}
    assert resolve_fast_mode_overrides(model, **kwargs) is None
    assert seen == []
    config["providers"]["test"]["capabilities"]["allow_paid_priority"] = True
    atomic_config_write(config_path, config)
    assert resolve_fast_mode_overrides(model, **kwargs) == {"service_tier": "priority"}
    assert seen == ["extended-key"]
    assert resolve_fast_mode_overrides(model, **{**kwargs, "api_key": "limited-key"}) is None
    model_metadata._endpoint_model_metadata_cache.clear()
    model_metadata._endpoint_model_metadata_cache_time.clear()
    assert resolve_fast_mode_overrides(model, **kwargs) == {"service_tier": "priority"}
    assert resolve_fast_mode_overrides(model, **{**kwargs, "api_key": "limited-key"}) is None
    assert seen == ["extended-key", "limited-key"]
    for changes in ({"provider": "other"}, {"api_mode": "codex_responses"}, {"api_key": None}, {"tier": "ultrafast"}):
        assert resolve_fast_mode_overrides(model, **{**kwargs, **changes}) is None
    other_home = tmp_path / "other-profile"
    atomic_config_write(other_home / "config.yaml", {"providers": {"test": {
        "api": endpoint, "capabilities": {"allow_paid_priority": False},
    }}})
    home_token = set_hermes_home_override(other_home)
    try:
        assert resolve_fast_mode_overrides(model, **kwargs) is None
    finally:
        reset_hermes_home_override(home_token)
    assert resolve_fast_mode_overrides(model, **kwargs) == {"service_tier": "priority"}
    config["providers"]["test"]["capabilities"]["allow_paid_priority"] = False
    config["providers"]["other"] = {"api": endpoint, "capabilities": {"allow_paid_priority": True}}
    atomic_config_write(config_path, config)
    assert resolve_fast_mode_overrides(model, **kwargs) is None
    config["providers"]["test"]["capabilities"]["allow_paid_priority"] = True
    atomic_config_write(config_path, config)
    assert resolve_fast_mode_overrides(model, **{**kwargs, "base_url": endpoint + "/other"}) is None
    # The namespace-derived bare alias is not an authoritative row for a paid capability.
    assert resolve_fast_mode_overrides("gpt-6.1-sol", **kwargs) is None
    assert resolve_fast_mode_overrides(model + "-neighbor", **kwargs) is None
    for malformed_or_denied in ([], None, "priority", ["priority", {}]):
        catalogs["extended-key"] = malformed_or_denied
        model_metadata.fetch_endpoint_model_metadata(endpoint, api_key="extended-key", force_refresh=True)
        assert resolve_fast_mode_overrides(model, **kwargs) is None
    cache_text = (tmp_path / "cache" / "endpoint_model_metadata.json")
    if cache_text.exists():
        assert "extended-key" not in cache_text.read_text(encoding="utf-8")


def test_custom_fast_modes_reach_wire_without_changing_prompt(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from tui_gateway import server

    endpoint, model = "https://catalog.example.invalid/v1", "tenant/gpt-6.1-sol"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_config_write(tmp_path / "config.yaml", {"providers": {"test": {
        "api": endpoint, "key_env": "CUSTOM_PRIORITY_TEST_KEY", "capabilities": {"allow_paid_priority": True},
    }}})
    monkeypatch.setenv("CUSTOM_PRIORITY_TEST_KEY", "extended-key")
    runtime = resolve_runtime_provider(requested="test", target_model=model)
    assert runtime["base_url"] == endpoint and runtime["api_key"] == "extended-key"
    wire = []

    def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model, "service_tiers": ["priority"]}]})
        body = json.loads(request.content)
        wire.append(body)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache_time", {})
    agent = SimpleNamespace(model=model, provider=runtime["provider"], requested_provider=runtime["requested_provider"], base_url=runtime["base_url"],
        api_key=runtime["api_key"], api_mode=runtime["api_mode"], acp_command=None, acp_args=[],
        service_tier="priority", request_overrides={"extra_body": {"retained": True}},
        fast_auto_seconds=60, session_id="test-priority")
    route = CLIAgentSetupMixin._resolve_turn_agent_config(agent, "hello")
    assert route["request_overrides"] == {"service_tier": "priority"}
    from gateway.run import GatewayRunner
    gateway_route = GatewayRunner._resolve_turn_agent_config(
        SimpleNamespace(_service_tier="priority"), "hello", model,
        {**runtime, "request_overrides": {"extra_body": {"retained": True}}},
    )
    assert gateway_route["request_overrides"] == {"extra_body": {"retained": True}, "service_tier": "priority"}
    agent.request_overrides.update(route["request_overrides"])
    # A resumed Desktop agent stores the tier but may not have rebuilt its top-level override yet.
    agent.request_overrides.pop("service_tier")
    assert fast_mode.effective_request_overrides(agent).get("service_tier") == "priority"
    session = {"session_key": "test-priority", "agent": agent}
    monkeypatch.setitem(server._sessions, "priority-runtime", session)
    monkeypatch.setattr(server, "_persist_live_session_runtime", lambda session: None)
    monkeypatch.setattr(server, "_emit_session_info", lambda *args: None)
    messages = [{"role": "system", "content": "stable prefix"}, {"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    transport = ChatCompletionsTransport()
    # The SDK validates against the real httpx.Client type; metadata probes already warmed their memo.
    monkeypatch.setattr(httpx, "Client", real_client)
    client = OpenAI(api_key="extended-key", base_url=endpoint, http_client=real_client(transport=httpx.MockTransport(respond)))

    def request():
        kwargs = transport.build_kwargs(model, messages, tools=tools, base_url=endpoint,
            provider_name="custom", request_overrides=fast_mode.effective_request_overrides(agent))
        client.chat.completions.create(**kwargs)

    request()
    assert wire[-1]["service_tier"] == "priority"
    result = server._methods["config.set"]("normal", {"session_id": "priority-runtime", "key": "fast", "value": "normal"})
    assert result["result"]["value"] == "normal"
    request()
    assert "service_tier" not in wire[-1]
    result = server._methods["config.set"]("fast", {"session_id": "priority-runtime", "key": "fast", "value": "fast"})
    assert result["result"]["value"] == "fast"
    assert server._fast_tier_applies(agent, model, "test", route_known=True, tier="priority") is True
    request()
    assert wire[-1]["service_tier"] == "priority"
    agent.service_tier = "auto"
    agent.request_overrides.pop("service_tier", None)
    clock = [1000.0]
    monkeypatch.setattr(fast_mode.time, "monotonic", lambda: clock[0])
    fast_mode.begin_turn(agent, [])
    request()
    assert wire[-1]["service_tier"] == "priority"
    clock[0] += 61
    request()
    assert "service_tier" not in wire[-1]
    agent.service_tier = "cold"
    fast_mode.begin_turn(agent, messages)
    request()
    assert "service_tier" not in wire[-1]
    fast_mode.begin_turn(agent, [])
    request()
    assert wire[-1]["service_tier"] == "priority"
    # Static priority must never follow a model/provider switch onto a route without consent.
    agent.service_tier = "priority"
    agent.request_overrides["service_tier"] = "priority"
    agent.requested_provider = "untrusted"
    request()
    assert "service_tier" not in wire[-1]
    assert all(row["messages"] == messages and row["tools"] == tools and row["retained"] is True for row in wire)
    client.close()


@pytest.mark.parametrize("mode", ["priority", "auto", "cold"])
def test_custom_priority_is_revalidated_after_transport_switch(tmp_path, monkeypatch, mode):
    from agent.transports.codex import ResponsesApiTransport
    from hermes_cli.config import atomic_config_write

    endpoint, model = "https://catalog.example.invalid/v1", "tenant/gpt-6.1-sol"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_config_write(tmp_path / "config.yaml", {"providers": {"test": {
        "api": endpoint, "capabilities": {"allow_paid_priority": True},
    }}})
    wire = []

    def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": model, "service_tiers": ["priority"]}]})
        wire.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "response", "model": model,
            "status": "completed", "output": []})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache_time", {})
    agent = SimpleNamespace(model=model, provider="custom", requested_provider="test", base_url=endpoint,
        api_key="extended-key", api_mode="chat_completions", service_tier=mode,
        request_overrides={"service_tier": "priority", "extra_body": {"retained": True}})
    fast_mode.begin_turn(agent, [])
    assert fast_mode.effective_request_overrides(agent)["service_tier"] == "priority"
    monkeypatch.setattr(httpx, "Client", real_client)
    messages = [{"role": "system", "content": "stable prefix"}, {"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    transport = ResponsesApiTransport()
    with OpenAI(api_key=agent.api_key, base_url=endpoint,
                http_client=real_client(transport=httpx.MockTransport(respond))) as client:
        agent.api_mode = "codex_responses"
        kwargs = transport.build_kwargs(model, messages, tools=tools, base_url=endpoint, provider="custom",
            request_overrides=fast_mode.effective_request_overrides(agent))
        client.responses.create(**kwargs)
        assert "service_tier" not in wire[-1]
        # Explicit provider extra_body remains always-on, independently of the session toggle.
        agent.request_overrides["extra_body"]["service_tier"] = "priority"
        kwargs = transport.build_kwargs(model, messages, tools=tools, base_url=endpoint, provider="custom",
            request_overrides=fast_mode.effective_request_overrides(agent))
        client.responses.create(**kwargs)
        assert wire[-1]["service_tier"] == "priority"
    assert wire[0]["instructions"] == wire[1]["instructions"] == "stable prefix"
    assert wire[0]["input"] == wire[1]["input"] and wire[0]["tools"] == wire[1]["tools"]
    assert all(row["retained"] is True for row in wire)
    for api_mode in ("anthropic_messages", "bedrock_converse", "codex_app_server", "unknown", None):
        agent.api_mode = api_mode
        effective = fast_mode.effective_request_overrides(agent)
        assert "service_tier" not in effective and "speed" not in effective
        assert effective["extra_body"] == {"retained": True, "service_tier": "priority"}
    assert agent.request_overrides["service_tier"] == "priority"  # never mutate stored overrides
    # Native Anthropic uses its own endpoint, not the generic OpenAI client URL.
    agent.model, agent.provider, agent.api_mode = "claude-opus-5", "anthropic", "anthropic_messages"
    agent.base_url, agent._anthropic_base_url = endpoint, "https://api.anthropic.com"
    agent.request_overrides = {"speed": "fast"}
    assert fast_mode.effective_request_overrides(agent) == {"speed": "fast"}
    del agent._anthropic_base_url  # None/missing means the native SDK's first-party default
    assert fast_mode.effective_request_overrides(agent) == {"speed": "fast"}
    agent.model = "unsupported-model"
    agent.provider, agent.api_mode, agent.base_url = "openai-api", "codex_responses", "https://api.openai.com/v1"
    agent.request_overrides = {"service_tier": "priority"}
    assert fast_mode.effective_request_overrides(agent) == {}


def test_custom_priority_consent_follows_runtime_provider_precedence(tmp_path, monkeypatch):
    import pytest
    from hermes_cli.config import atomic_config_replace, atomic_config_write
    from hermes_cli.runtime_provider import resolve_runtime_provider

    endpoint, model = "https://catalog.example.invalid/v1", "tenant/gpt-6.1-sol"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config = {"providers": {"test": {"name": "Shared Route", "api": endpoint,
        "api_key": "modern-key", "capabilities": {"allow_paid_priority": False}}},
        "custom_providers": [{"name": "test", "base_url": endpoint, "api_key": "legacy-key",
            "capabilities": {"allow_paid_priority": True}}]}
    atomic_config_write(config_path, config)
    seen = []

    def respond(request):
        seen.append(request.headers.get("Authorization", "").removeprefix("Bearer "))
        return httpx.Response(200, json={"data": [{"id": model, "service_tiers": ["priority"]}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache_time", {})
    for provider in ("test", "custom:test", "Shared Route", "custom:shared-route"):
        runtime = resolve_runtime_provider(requested=provider, target_model=model)
        assert runtime["base_url"] == endpoint and isinstance(runtime["api_key"], str)
        assert runtime["capabilities"]["allow_paid_priority"] is False
        assert resolve_fast_mode_overrides(model, provider=provider, base_url=runtime["base_url"],
            api_key=runtime["api_key"], api_mode=runtime["api_mode"]) is None
    assert seen == []  # denial must precede any authenticated metadata probe
    config["providers"]["test"]["capabilities"]["allow_paid_priority"] = True
    config["custom_providers"][0]["capabilities"]["allow_paid_priority"] = False
    atomic_config_write(config_path, config)
    runtime = resolve_runtime_provider(requested="custom:test", target_model=model)
    kwargs = {"provider": "custom:test", "base_url": runtime["base_url"],
        "api_key": runtime["api_key"], "api_mode": runtime["api_mode"]}
    # Consent resolution must not independently read key_env or mint key_cmd credentials.
    config["providers"]["test"]["key_env"] = "DO_NOT_READ_PRIORITY_KEY"
    config["providers"]["test"]["key_cmd"] = "do-not-run-priority-token-command"
    atomic_config_write(config_path, config)
    with monkeypatch.context() as scoped:
        from hermes_cli import runtime_provider_custom
        from agent import command_token_source

        def forbidden(*args, **kwargs):
            raise AssertionError("priority consent must use the caller's resolved key")

        scoped.setattr(runtime_provider_custom, "get_secret_str", forbidden)
        scoped.setattr(command_token_source, "build_command_token_provider", forbidden)
        assert resolve_fast_mode_overrides(model, **kwargs) == {"service_tier": "priority"}
    assert seen == [runtime["api_key"]]
    # The selected modern identity still wins when a legacy row matches the old endpoint.
    config["providers"]["test"]["api"] = endpoint + "/modern"
    config["custom_providers"][0]["capabilities"]["allow_paid_priority"] = True
    atomic_config_write(config_path, config)
    assert resolve_runtime_provider(requested="test", explicit_api_key="modern-key", target_model=model)["base_url"] == endpoint + "/modern"
    assert resolve_fast_mode_overrides(model, **kwargs) is None
    # A disabled raw provider is refused, while its explicit custom alias can resolve the legacy entry.
    config["providers"]["test"]["enabled"] = False
    atomic_config_write(config_path, config)
    with pytest.raises(ValueError, match="disabled"):
        resolve_runtime_provider(requested="test", target_model=model)
    assert resolve_fast_mode_overrides(model, **{**kwargs, "provider": "test"}) is None
    runtime = resolve_runtime_provider(requested="custom:test", target_model=model)
    assert runtime["api_key"] == "legacy-key"
    assert resolve_fast_mode_overrides(model, **{**kwargs, "api_key": runtime["api_key"]}) == {"service_tier": "priority"}
    # Canonical built-ins shadow saved names; only the explicit custom identity opts into that entry.
    config = {"providers": {"nous": {"api": endpoint, "api_key": "modern-key",
        "capabilities": {"allow_paid_priority": True}}}}
    atomic_config_replace(config_path, config)
    from hermes_cli.runtime_provider import _get_named_custom_provider
    assert _get_named_custom_provider("nous") is None
    assert resolve_fast_mode_overrides(model, **{**kwargs, "provider": "nous"}) is None
    assert _get_named_custom_provider("custom:nous")["base_url"] == endpoint
    assert resolve_fast_mode_overrides(model, **{**kwargs, "provider": "custom:nous"}) == {"service_tier": "priority"}


def _snapshot_overrides(overrides):
    # deepcopy cannot pickle MappingProxyType; snapshot its contents instead.
    return copy.deepcopy({key: dict(value) if key in ("extra_body", "extra_headers") else value
                          for key, value in overrides.items()})


@pytest.fixture
def priority_wire(tmp_path, monkeypatch):
    """Real transport + SDK; only HTTP I/O is replaced, with synthetic route credentials."""
    from hermes_cli.config import atomic_config_write
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from providers import get_provider_profile

    endpoint = "https://wire.example.invalid/v1"
    selected = "tenant/gpt-6.1-sol"
    allowed = "tenant/opaque-priority"
    denied = "tenant/opaque-standard"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_config_write(tmp_path / "config.yaml", {"providers": {"wire": {
        "api": endpoint, "api_key": "wire-synthetic-key",
        "capabilities": {"allow_paid_priority": True},
    }}})
    runtime = resolve_runtime_provider(requested="wire", target_model=selected)
    wire, catalog_keys = [], []

    def respond(request):
        assert request.url.host == "wire.example.invalid"
        assert request.headers["Authorization"] == "Bearer wire-synthetic-key"
        if request.url.path.endswith("/models"):
            catalog_keys.append(request.headers["Authorization"])
            return httpx.Response(200, json={"data": [
                {"id": selected, "service_tiers": ["priority"]},
                {"id": allowed, "service_tiers": ["priority"]},
                {"id": denied, "service_tiers": []},
            ]})
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        wire.append(body)
        return httpx.Response(200, json={"id": "wire-test", "object": "chat.completion",
            "model": body["model"], "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    real_client = httpx.Client
    with monkeypatch.context() as scoped:
        scoped.setattr(httpx, "Client", lambda **kwargs: real_client(
            **{**kwargs, "transport": httpx.MockTransport(respond)},
        ))
        scoped.setattr(model_metadata, "_endpoint_model_metadata_cache", {})
        scoped.setattr(model_metadata, "_endpoint_model_metadata_cache_time", {})
        catalog = model_metadata.fetch_endpoint_model_metadata(endpoint, api_key=runtime["api_key"])
        assert all(catalog[model]["id"] == model for model in (selected, allowed, denied))
        # Restore Client's class for the SDK's isinstance check without replacing real policy.
        scoped.setattr(httpx, "Client", real_client)
        clock = [1000.0]
        scoped.setattr(fast_mode.time, "monotonic", lambda: clock[0])
        messages = [{"role": "system", "content": "byte-stable prefix\nНикаких изменений"},
            {"role": "user", "content": "lookup"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-wire",
                "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-wire", "content": "known result"}]
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        prompt = copy.deepcopy((messages, tools))
        agent = SimpleNamespace(model=selected, provider=runtime["provider"],
            requested_provider=runtime["requested_provider"], base_url=runtime["base_url"],
            api_key=runtime["api_key"], api_mode=runtime["api_mode"],
            service_tier=None, request_overrides={}, fast_auto_seconds=60)
        transport = ChatCompletionsTransport()
        profile = copy.copy(get_provider_profile("custom"))
        profile.supports_prompt_cache_key = True
        with OpenAI(api_key=runtime["api_key"], base_url=endpoint,
                    http_client=real_client(transport=httpx.MockTransport(respond))) as client:
            def send(profile_path, **params):
                stored = _snapshot_overrides(agent.request_overrides)
                original_body = agent.request_overrides.get("extra_body")
                overrides = fast_mode.effective_request_overrides(agent)
                if profile_path == "production":
                    from agent.chat_completion_helpers import _build_chat_completions_kwargs

                    agent._get_transport = lambda: transport
                    agent._base_url_lower = endpoint.lower()
                    agent._base_url_hostname = "wire.example.invalid"
                    agent._is_qwen_portal = lambda: False
                    agent._is_openrouter_url = lambda: False
                    agent._prepare_messages_for_non_vision_model = lambda rows: rows
                    agent._resolved_api_call_timeout = lambda: 30
                    agent._max_tokens_param = lambda cap: {"max_tokens": cap}
                    agent._supports_reasoning_extra_body = lambda: True
                    agent._ollama_num_ctx = params.get("ollama_num_ctx")
                    agent.max_tokens = 1024
                    agent.session_id = "wire-session"
                    agent.openrouter_min_coding_score = None
                    for name in ("providers_allowed", "providers_ignored", "providers_order",
                                 "provider_sort", "provider_require_parameters", "provider_data_collection"):
                        setattr(agent, name, None)
                    kwargs = _build_chat_completions_kwargs(
                        agent, messages, tools, params.get("reasoning_config"), overrides, "wire-scope",
                    )
                else:
                    params = {**({"provider_profile": profile} if profile_path else {}), **params}
                    kwargs = transport.build_kwargs(selected, messages, tools=tools, base_url=endpoint,
                        provider_name="custom", session_id="wire-session", supports_prompt_cache_key=True,
                        request_overrides=overrides, **params)
                client.chat.completions.create(**kwargs)
                assert _snapshot_overrides(agent.request_overrides) == stored
                assert agent.request_overrides.get("extra_body") is original_body
                assert (messages, tools) == prompt
                return wire[-1]

            yield SimpleNamespace(agent=agent, send=send, wire=wire, catalog_keys=catalog_keys,
                selected=selected, allowed=allowed, denied=denied, clock=clock, messages=messages)


@pytest.mark.parametrize("mode", ["priority", "auto", "cold"])
@pytest.mark.parametrize("profile_path", [False, True], ids=["legacy", "profile"])
@pytest.mark.parametrize("top_target,body_target,permitted", [
    ("denied", None, False), ("allowed", None, True),
    (None, "denied", False), (None, "allowed", True),
    ("allowed", "denied", False), ("denied", "allowed", True),
    (None, "gpt-6.1-sol", False), (None, "tenant/missing", False),
    (None, " tenant/opaque-priority ", False), (None, "TENANT/opaque-priority", False),
    (None, "", False), (None, 42, False),
])
def test_generated_priority_validates_the_final_sdk_model(
    priority_wire, mode, profile_path, top_target, body_target, permitted,
):
    case = priority_wire
    agent = case.agent
    top_model = getattr(case, top_target, top_target) if top_target is not None else None
    body_model = getattr(case, body_target, body_target) if isinstance(body_target, str) else body_target
    # Include opposing layers: the SDK's extra_body.model, not the top-level model, wins.
    expected_model = body_model if body_target is not None else top_model
    agent.request_overrides = {"extra_body": {"retained": {"opaque": True}}}
    if top_target is not None:
        agent.request_overrides["model"] = top_model
    if body_target is not None:
        agent.request_overrides["extra_body"]["model"] = body_model
    normal = case.send(profile_path)
    assert normal["model"] == expected_model and "service_tier" not in normal
    agent.service_tier = mode
    if mode == "priority":
        agent.request_overrides["service_tier"] = "priority"  # previously generated static tier
    fast_mode.begin_turn(agent, [])
    active = case.send(profile_path)
    assert active["model"] == expected_model
    assert active.get("service_tier") == ("priority" if permitted else None)
    assert {k: v for k, v in active.items() if k != "service_tier"} == normal
    assert active["messages"][0]["content"].encode() == normal["messages"][0]["content"].encode()
    assert active["prompt_cache_key"] == normal["prompt_cache_key"]
    if mode in fast_mode.BOUNDED_MODES:
        case.clock[0] += 61
        assert case.send(profile_path) == normal
        if mode == "cold":
            fast_mode.begin_turn(agent, normal["messages"])
            assert case.send(profile_path) == normal
    # Normal removes only the generated session field; model overrides are untouched.
    agent.service_tier = None
    agent.request_overrides.pop("service_tier", None)
    assert case.send(profile_path) == normal
    assert case.catalog_keys == ["Bearer wire-synthetic-key"]


@pytest.mark.parametrize("mode", ["priority", "auto", "cold"])
@pytest.mark.parametrize("profile_path", [False, True], ids=["legacy", "profile"])
def test_sdk_mapping_extra_body_model_also_controls_generated_priority(priority_wire, mode, profile_path):
    from collections import UserDict

    case = priority_wire
    case.agent.service_tier = mode
    case.agent.request_overrides = {"model": case.allowed,
        "extra_body": UserDict({"model": case.denied, "retained": True})}
    fast_mode.begin_turn(case.agent, [])
    wire = case.send(profile_path)
    assert wire["model"] == case.denied and "service_tier" not in wire


@pytest.mark.parametrize("mode", ["priority", "auto", "cold", "ultrafast"])
@pytest.mark.parametrize("top_model,body_model,expected_tier", [
    ("gpt-6-astra-900k", None, True),
    ("unsupported-model", None, False),
    ("gpt-6-astra-900k", "unsupported-model", False),
    ("unsupported-model", "gpt-6-astra", True),
])
def test_first_party_responses_generated_tier_uses_sdk_wire_model(
    mode, top_model, body_model, expected_tier, monkeypatch,
):
    from agent.transports.codex import ResponsesApiTransport

    clock = [1000.0]
    monkeypatch.setattr(fast_mode.time, "monotonic", lambda: clock[0])
    wire = []

    def respond(request):
        assert request.url.host == "api.openai.com" and request.url.path.endswith("/responses")
        body = json.loads(request.content)
        wire.append(body)
        return httpx.Response(200, json={"id": "wire-test", "object": "response",
            "model": body["model"], "status": "completed", "output": []})

    agent = SimpleNamespace(model="gpt-6-astra", provider="openai-api", api_mode="codex_responses",
        api_key="first-party-synthetic-key", base_url="https://api.openai.com/v1", service_tier=None,
        request_overrides={"model": top_model, "extra_body": {"retained": True}})
    if body_model is not None:
        agent.request_overrides["extra_body"]["model"] = body_model
    messages = [{"role": "system", "content": "stable responses prefix"}, {"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    prompt = copy.deepcopy((messages, tools))
    transport = ResponsesApiTransport()
    with OpenAI(api_key=agent.api_key, base_url=agent.base_url,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        def send():
            stored = copy.deepcopy(agent.request_overrides)
            kwargs = transport.build_kwargs(agent.model, messages, tools=tools, base_url=agent.base_url,
                provider=agent.provider, session_id="wire-session",
                request_overrides=fast_mode.effective_request_overrides(agent))
            client.responses.create(**kwargs)
            assert agent.request_overrides == stored and (messages, tools) == prompt
            return wire[-1]

        normal = send()
        agent.service_tier = mode
        agent.request_overrides["service_tier"] = "priority"  # stale generated field
        fast_mode.begin_turn(agent, [])
        active = send()
        expected_model = body_model or model_metadata.strip_codex_context_variant_suffix(top_model)
        assert active["model"] == expected_model
        expected = ("ultrafast" if mode == "ultrafast" else "priority") if expected_tier else None
        assert active.get("service_tier") == expected
        assert {k: v for k, v in active.items() if k != "service_tier"} == normal
        if mode in fast_mode.BOUNDED_MODES:
            clock[0] += 61
            assert send() == normal


@pytest.mark.parametrize("mode", ["priority", "auto", "cold", "ultrafast"])
def test_custom_generated_tier_fails_closed_on_unsupported_transport(priority_wire, mode):
    case = priority_wire
    case.agent.service_tier = mode
    case.agent.request_overrides = {"service_tier": "priority", "speed": "fast",
        "model": case.allowed, "extra_body": {"retained": True}}
    fast_mode.begin_turn(case.agent, [])
    for api_mode in ("codex_responses", "anthropic_messages", "bedrock_converse", "codex_app_server", "unknown", None):
        case.agent.api_mode = api_mode
        effective = fast_mode.effective_request_overrides(case.agent)
        assert effective == {"model": case.allowed, "extra_body": {"retained": True}}
    case.agent.api_mode = "chat_completions"
    wire = case.send(True)
    assert wire.get("service_tier") == (None if mode == "ultrafast" else "priority")
    if mode in fast_mode.BOUNDED_MODES:
        case.clock[0] += 61
        assert "service_tier" not in case.send(True)  # stale top-level tier does not outlive the window


@pytest.mark.parametrize("mode", [None, "priority", "ultrafast", "auto", "cold"])
def test_explicit_extra_body_priority_remains_always_on(priority_wire, mode):
    case = priority_wire
    case.agent.service_tier = mode
    case.agent.request_overrides = {"model": case.allowed,
        "extra_body": {"model": case.denied, "service_tier": "priority", "retained": True}}
    fast_mode.begin_turn(case.agent, [])
    active = case.send(True)
    assert active["model"] == case.denied and active["service_tier"] == "priority"
    case.clock[0] += 61
    assert case.send(True) == active
    case.agent.service_tier = None
    assert case.send(True) == active


@pytest.mark.parametrize("body_type", [dict, UserDict, MappingProxyType], ids=["dict", "userdict", "proxy"])
@pytest.mark.parametrize("mode", [None, "priority", "auto", "cold"], ids=["normal", "priority", "auto", "cold"])
@pytest.mark.parametrize("allowed_body", [False, True], ids=["allowed-to-denied", "denied-to-allowed"])
@pytest.mark.parametrize("explicit_tier", [False, True], ids=["generated", "always-on"])
@pytest.mark.parametrize("merge_path", ["legacy", "profile-ctx", "profile-additions", "profile-reasoning", "production"])
def test_mapping_profile_merge_preserves_final_sdk_model(
    priority_wire, body_type, mode, allowed_body, explicit_tier, merge_path,
):
    from providers import get_provider_profile

    case = priority_wire
    body_model = case.allowed if allowed_body else case.denied
    body = {"model": body_model, "retained": {"opaque": True}, "prompt_cache_key": "explicit-cache"}
    if explicit_tier:
        body["service_tier"] = "priority"
    case.agent.request_overrides = {"model": case.denied if allowed_body else case.allowed,
        "extra_body": body_type(body)}
    profile_path = merge_path != "legacy"
    params = {}
    if merge_path in ("profile-ctx", "production"):
        params["ollama_num_ctx"] = 4096
    elif merge_path in ("legacy", "profile-additions"):
        params["extra_body_additions"] = {"retained": "profile-default", "addition": {"kept": True}}
    else:
        params.update(provider_profile=get_provider_profile("openrouter"), supports_reasoning=True,
                      reasoning_config={"enabled": True, "effort": "medium"})
    if merge_path == "production":
        profile_path = "production"
    original_params = copy.deepcopy({key: value for key, value in params.items() if key != "provider_profile"})
    normal = case.send(profile_path, **params)
    assert normal["model"] == body_model
    assert normal["retained"] == {"opaque": True}
    assert normal["prompt_cache_key"] == "explicit-cache"
    assert normal.get("service_tier") == ("priority" if explicit_tier else None)
    if "ollama_num_ctx" in params:
        assert normal["options"] == {"num_ctx": 4096}
    elif "extra_body_additions" in params and profile_path:
        assert normal["addition"] == {"kept": True}
    elif merge_path == "profile-reasoning":
        assert normal["reasoning"]["effort"] == "medium"
    case.agent.service_tier = mode
    if mode == "priority":
        case.agent.request_overrides["service_tier"] = "priority"
    fast_mode.begin_turn(case.agent, [])
    active = case.send(profile_path, **params)
    expected = explicit_tier or (mode is not None and allowed_body)
    assert active["model"] == body_model
    assert active.get("service_tier") == ("priority" if expected else None)
    assert {k: v for k, v in active.items() if k != "service_tier"} == {
        k: v for k, v in normal.items() if k != "service_tier"}
    if mode in fast_mode.BOUNDED_MODES:
        case.clock[0] += 61
        assert case.send(profile_path, **params) == normal
        fast_mode.begin_turn(case.agent, case.messages)
        warm = case.send(profile_path, **params)
        warm_priority = explicit_tier or (mode == "auto" and allowed_body)
        assert warm.get("service_tier") == ("priority" if warm_priority else None)
    case.agent.service_tier = None
    case.agent.request_overrides.pop("service_tier", None)
    assert case.send(profile_path, **params) == normal
    assert {key: value for key, value in params.items() if key != "provider_profile"} == original_params


@pytest.mark.parametrize("body_type", [dict, UserDict, MappingProxyType], ids=["dict", "userdict", "proxy"])
@pytest.mark.parametrize("mode", [None, "priority", "auto", "cold"], ids=["normal", "priority", "auto", "cold"])
@pytest.mark.parametrize("allowed_body", [False, True], ids=["allowed-to-denied", "denied-to-allowed"])
@pytest.mark.parametrize("explicit_tier", [False, True], ids=["generated", "always-on"])
@pytest.mark.parametrize("session_id", [None, "wire-session"], ids=["no-session", "session"])
@pytest.mark.parametrize("preflight", [False, True], ids=["sdk-direct", "preflight"])
def test_mapping_xai_session_merge_preserves_final_sdk_model(
    monkeypatch, body_type, mode, allowed_body, explicit_tier, session_id, preflight,
):
    from agent.transports.codex import ResponsesApiTransport

    allowed, denied = "grok-4.6", "unsupported-model"
    body_model = allowed if allowed_body else denied
    body = {"model": body_model, "retained": {"opaque": True}, "prompt_cache_key": "body-cache"}
    if explicit_tier:
        body["service_tier"] = "priority"
    agent = SimpleNamespace(model=allowed, provider="xai", api_mode="codex_responses",
        base_url="https://api.x.ai/v1", api_key="xai-synthetic-key", service_tier=None,
        request_overrides={"model": denied if allowed_body else allowed,
            "prompt_cache_key": "top-cache", "extra_body": body_type(body),
            "extra_headers": body_type({"x-retained": "caller-header", "x-grok-conv-id": "stale-scope"})},
        fast_auto_seconds=60)
    clock = [1000.0]
    monkeypatch.setattr(fast_mode.time, "monotonic", lambda: clock[0])
    wire = []

    def respond(request):
        assert request.url.host == "api.x.ai" and request.url.path.endswith("/responses")
        payload = json.loads(request.content)
        wire.append((payload, dict(request.headers)))
        return httpx.Response(200, json={"id": "wire-xai", "object": "response",
            "model": payload["model"], "status": "completed", "output": []})

    messages = [{"role": "system", "content": "stable xAI prefix"}, {"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    prompt = copy.deepcopy((messages, tools))
    transport = ResponsesApiTransport()
    with OpenAI(api_key=agent.api_key, base_url=agent.base_url,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        def send():
            stored = _snapshot_overrides(agent.request_overrides)
            original_body = agent.request_overrides["extra_body"]
            original_headers = agent.request_overrides["extra_headers"]
            kwargs = transport.build_kwargs(agent.model, messages, tools=tools,
                base_url=agent.base_url, provider=agent.provider, is_xai_responses=True,
                session_id=session_id, cache_scope_id="rotation-stable-scope",
                request_overrides=fast_mode.effective_request_overrides(agent))
            client.responses.create(**(transport.preflight_kwargs(kwargs) if preflight else kwargs))
            assert _snapshot_overrides(agent.request_overrides) == stored
            assert agent.request_overrides["extra_body"] is original_body
            assert agent.request_overrides["extra_headers"] is original_headers
            assert (messages, tools) == prompt
            payload, headers = wire[-1]
            assert payload["retained"] == {"opaque": True}
            assert payload["prompt_cache_key"] == "body-cache"
            assert headers["x-retained"] == "caller-header"
            assert headers["x-grok-conv-id"] == ("rotation-stable-scope" if session_id else "stale-scope")
            return payload

        normal = send()
        assert normal["model"] == body_model
        assert normal.get("service_tier") == ("priority" if explicit_tier else None)
        agent.service_tier = mode
        if mode == "priority":
            agent.request_overrides["service_tier"] = "priority"
        fast_mode.begin_turn(agent, [])
        active = send()
        assert active["model"] == body_model
        expected = explicit_tier or (mode is not None and allowed_body)
        assert active.get("service_tier") == ("priority" if expected else None)
        assert {k: v for k, v in active.items() if k != "service_tier"} == {
            k: v for k, v in normal.items() if k != "service_tier"}
        if mode in fast_mode.BOUNDED_MODES:
            clock[0] += 61
            assert send() == normal
            fast_mode.begin_turn(agent, messages)
            warm = send()
            warm_priority = explicit_tier or (mode == "auto" and allowed_body)
            assert warm.get("service_tier") == ("priority" if warm_priority else None)
        agent.service_tier = None
        agent.request_overrides.pop("service_tier", None)
        assert send() == normal


@pytest.mark.parametrize("body_type", [dict, UserDict, MappingProxyType], ids=["dict", "userdict", "proxy"])
@pytest.mark.parametrize("route", ["chat-legacy", "chat-profile", "responses", "xai-no-session", "xai-session"])
@pytest.mark.parametrize("key", ["explicit-cache", "c" * 100, ""], ids=["short", "long", "empty"])
def test_mapping_cache_defaults_bound_request_copy_without_mutation(body_type, route, key):
    from agent.transports.codex import ResponsesApiTransport, _bounded_prompt_cache_key
    from providers import get_provider_profile

    is_chat = route.startswith("chat")
    is_xai = route.startswith("xai")
    transport = ChatCompletionsTransport() if is_chat else ResponsesApiTransport()
    messages = [{"role": "system", "content": "stable prefix"}, {"role": "user", "content": "hello"}]
    overrides = {"extra_body": body_type({"model": "grok-4.6", "retained": True, "prompt_cache_key": key})}
    stored = _snapshot_overrides(overrides)
    body = overrides["extra_body"]
    wire = []

    def respond(request):
        payload = json.loads(request.content)
        wire.append(payload)
        if is_chat:
            return httpx.Response(200, json={"id": "wire-cache", "object": "chat.completion",
                "model": payload["model"], "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"id": "wire-cache", "object": "response",
            "model": payload["model"], "status": "completed", "output": []})

    params = {}
    if route == "chat-profile":
        params.update(provider_profile=get_provider_profile("custom"), ollama_num_ctx=4096)
    if not is_chat:
        params.update(is_xai_responses=is_xai, session_id="cache-session" if route == "xai-session" else None)
    kwargs = transport.build_kwargs("grok-4.6", messages, base_url="https://cache.example.invalid/v1",
        request_overrides=overrides, **params)
    with OpenAI(api_key="cache-synthetic-key", base_url="https://cache.example.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        if is_chat:
            client.chat.completions.create(**kwargs)
        else:
            client.responses.create(**transport.preflight_kwargs(kwargs))
    assert wire[-1]["model"] == "grok-4.6" and wire[-1]["retained"] is True
    expected_key = _bounded_prompt_cache_key(key) or kwargs.get("prompt_cache_key")
    assert wire[-1].get("prompt_cache_key") == expected_key
    assert _snapshot_overrides(overrides) == stored and overrides["extra_body"] is body
