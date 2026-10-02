"""Opted-in priority for named custom Chat Completions routes."""
from __future__ import annotations

from typing import Any


def resolve_custom_priority_overrides(
    model_id: str | None, *, provider: str | None, base_url: str | None,
    api_key: object, api_mode: str | None, tier: str | None,
) -> dict[str, Any] | None:
    """A paid tier requires explicit consent and the actual key's exact catalog row.

    Read settings in the caller's profile scope; never independently resolve credentials or
    infer consent from a model family, another provider at the same URL, or a generated alias.
    """
    if not model_id or not provider or not base_url or api_mode != "chat_completions":
        return None
    if tier not in (None, "priority") or not isinstance(api_key, str) or not api_key:
        return None

    from hermes_cli.config import get_compatible_custom_providers, is_provider_enabled, load_config_readonly
    from hermes_cli.providers import custom_provider_aliases
    from hermes_cli.route_identity import normalize_route_base_url
    from hermes_cli.runtime_provider_custom import _shadowed_by_builtin

    config = load_config_readonly()
    route = normalize_route_base_url(base_url)
    raw_identity = str(provider).strip().lower()
    identity = raw_identity.replace(" ", "-")
    providers = config.get("providers")
    providers = providers if isinstance(providers, dict) else {}
    if not is_provider_enabled(providers.get(raw_identity)):
        return None
    if identity == "auto" or _shadowed_by_builtin(identity):
        return None

    # Match the runtime's modern-first identity selection BEFORE checking the endpoint.
    # The compatibility list is legacy-first and can hide a modern explicit denial.
    selected, selected_url = None, None
    for provider_key, entry in providers.items():
        if not isinstance(entry, dict) or not is_provider_enabled(entry):
            continue
        if identity not in custom_provider_aliases(str(entry.get("name") or provider_key), str(provider_key)):
            continue
        selected_url = entry.get("api") or entry.get("url") or entry.get("base_url")
        if selected_url:
            selected = entry
            break
    if selected is None:
        if isinstance(config.get("custom_providers"), dict):
            return None
        for entry in get_compatible_custom_providers(config):
            name, url = entry.get("name"), entry.get("base_url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            if identity in custom_provider_aliases(name, entry.get("provider_key", "")):
                selected, selected_url = entry, url
                break
    if selected is None or normalize_route_base_url(selected_url) != route:
        return None
    capabilities = selected.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("allow_paid_priority") is not True:
        return None

    from agent.model_metadata import fetch_endpoint_model_metadata

    entry = fetch_endpoint_model_metadata(base_url, api_key=api_key).get(model_id)
    if not isinstance(entry, dict) or entry.get("id") != model_id:
        return None
    tiers = entry.get("service_tiers")
    if not isinstance(tiers, list) or "priority" not in tiers:
        return None
    return {"service_tier": "priority"}
