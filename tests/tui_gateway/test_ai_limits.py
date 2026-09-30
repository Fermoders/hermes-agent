from tui_gateway import server

def test_ai_limits_get_discovers_provider_by_endpoint_capability(monkeypatch):
    providers = [
        {
            "name": "renamed-by-user",
            "base_url": "https://limits.example/v1",
            "api_key": "secret-key",
        }
    ]
    requested = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, **_kwargs):
            requested.append(url)
            if url.endswith("/v0/user/ai-limits"):
                return Response({"models": {}})
            if url.endswith("/v0/user/usage"):
                return Response({"requests": 1})
            raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.iter_ai_limits_provider_runtimes",
        lambda: providers,
    )
    monkeypatch.setattr("httpx.Client", Client)

    result = server._methods["ai_limits.get"]("limits", {})

    assert result["result"]["base_url"] == "https://limits.example/v1"
    assert result["result"]["limits"] == {"models": {}}
    assert result["result"]["usage"] == {"requests": 1}
    assert requested == [
        "https://limits.example/v0/user/ai-limits",
        "https://limits.example/v0/user/usage",
    ]


def test_ai_limits_provider_discovery_ignores_custom_provider_name(monkeypatch):
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "load_config",
        lambda: {
            "providers": {
                "anything-the-user-wants": {
                    "api": "https://limits.example/v1",
                    "api_key": "secret-key",
                }
            }
        },
    )

    assert runtime_provider.iter_ai_limits_provider_runtimes() == [
        {"base_url": "https://limits.example/v1", "api_key": "secret-key"}
    ]
