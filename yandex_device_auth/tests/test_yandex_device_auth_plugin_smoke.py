from __future__ import annotations

import pytest

from sdk.testing import PluginTestRuntime


def test_metadata_smoke() -> None:
    from plugins.yandex_device_auth.plugin import YandexDeviceAuthPlugin

    plugin = YandexDeviceAuthPlugin(PluginTestRuntime())
    md = plugin.metadata
    assert md.name == "yandex_device_auth"
    assert "yandex:session_cookies" in md.capabilities_provided


@pytest.mark.asyncio
async def test_on_load_registers_expected_services(monkeypatch) -> None:
    from plugins.yandex_device_auth.plugin import YandexDeviceAuthPlugin

    runtime = PluginTestRuntime()
    plugin = YandexDeviceAuthPlugin(runtime)

    # Make on_load lightweight: it tries to update inspector flows via context.state.
    async def _noop_sync(*a, **k):
        return None

    monkeypatch.setattr(plugin, "_sync_auth_inspector_flows", _noop_sync)

    await plugin.on_load()

    # Primary entrypoints (both namespaced + legacy aliases)
    assert "yandex_device_auth.start" in runtime.registered_services
    assert "yandex_device_auth.status" in runtime.registered_services
    assert "yandex_device_auth.cookies" in runtime.registered_services
    assert "yandex_device_auth.unlink" in runtime.registered_services
    assert "device_auth.start" in runtime.registered_services
    assert "device_auth.status" in runtime.registered_services

