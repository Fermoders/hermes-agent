"""Model Settings must probe the same scoped route/credential as a new chat."""
from __future__ import annotations

from contextlib import contextmanager

import httpx

from agent import model_metadata, secret_scope
from hermes_cli.config import atomic_config_write, load_config
from hermes_cli.web_routers import models as router
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_model_info_uses_profile_runtime_credential_for_context(tmp_path, monkeypatch):
    endpoint, model = "https://catalog.example.invalid/v1", "tenant/gpt-6.1-sol"
    homes = {"A": tmp_path / "profile-a", "B": tmp_path / "profile-b"}
    keys = {"A": "extended-key", "B": "limited-key"}
    contexts = {"extended-key": 872_000, "limited-key": 256_000}
    for home in homes.values():
        atomic_config_write(home / "config.yaml", {
            "model": {"provider": "test", "default": model},
            "providers": {"test": {"api": endpoint, "key_env": "CATALOG_PROFILE_KEY"}},
        })
    seen = []

    @contextmanager
    def scope(profile):
        home_token = set_hermes_home_override(homes[profile])
        secret_token = secret_scope.set_secret_scope({"CATALOG_PROFILE_KEY": keys[profile]}, profile_home=str(homes[profile]))
        try:
            yield homes[profile]
        finally:
            secret_scope.reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)

    def scoped_config(profile):
        with scope(profile):
            return load_config()

    def respond(request):
        key = request.headers.get("Authorization", "").removeprefix("Bearer ")
        seen.append(key)
        if key not in contexts:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": model, "context_length": contexts[key]}]})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    monkeypatch.setattr(router, "_config_profile_scope", scope)
    monkeypatch.setattr(router, "_load_config_scoped", scoped_config)
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_endpoint_model_metadata_cache_time", {})
    for profile in ("A", "B", "A"):
        info = router.get_model_info(profile=profile)
        assert info["auto_context_length"] == contexts[keys[profile]]
        assert info["effective_context_length"] == contexts[keys[profile]]
        assert info["provider"] == "test" and info["model"] == model
    assert seen == [keys["A"], keys["B"]]
