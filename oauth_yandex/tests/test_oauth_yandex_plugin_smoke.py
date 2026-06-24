from __future__ import annotations

import pytest

from sdk.testing import PluginTestRuntime


def test_metadata_smoke() -> None:
    from plugins.oauth_yandex.plugin import OAuthYandexPlugin

    plugin = OAuthYandexPlugin(PluginTestRuntime())
    md = plugin.metadata
    assert md.name == "oauth_yandex"
    assert "oauth:yandex" in md.capabilities_provided


@pytest.mark.asyncio
async def test_on_load_registers_expected_services_and_handlers(monkeypatch) -> None:
    import plugins.oauth_yandex.plugin as plugin_mod
    import plugins.oauth_yandex.login_flow as login_flow_mod

    class _StubLoginService:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            return {"ok": True}

        async def status(self):
            return {"ok": True}

    monkeypatch.setattr(login_flow_mod, "YandexLoginService", _StubLoginService)

    runtime = PluginTestRuntime()
    plugin = plugin_mod.OAuthYandexPlugin(runtime)
    await plugin.on_load()

    # Public capability services
    assert "oauth_yandex.get_status" in runtime.registered_services
    assert "oauth_yandex.get_access_token" in runtime.registered_services

    # Cookie services used by Quasar integration
    assert "oauth_yandex.get_cookies" in runtime.registered_services
    assert "oauth_yandex.set_cookies" in runtime.registered_services

    # Operation handler for refresh pipeline
    assert "oauth.refresh_token" in runtime.registered_operation_handlers

