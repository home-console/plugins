from __future__ import annotations

import asyncio

import pytest

from sdk.testing import PluginTestRuntime
from plugins.yandex_smart_home.plugin import YandexSmartHomeRealPlugin


def test_metadata_smoke() -> None:
    plugin = YandexSmartHomeRealPlugin(PluginTestRuntime())
    md = plugin.metadata
    assert md.name == "yandex_smart_home"
    assert "oauth:yandex" in md.capabilities_required
    assert "yandex:session_cookies" in md.capabilities_required


@pytest.mark.asyncio
async def test_on_load_registers_expected_services(monkeypatch) -> None:
    runtime = PluginTestRuntime()
    plugin = YandexSmartHomeRealPlugin(runtime)

    # Make on_load lightweight: skip real network work.
    async def _noop_sync():
        return {"status": "skipped"}

    async def _noop_loop():
        return None

    monkeypatch.setattr(plugin, "_sync_devices_internal", _noop_sync)
    monkeypatch.setattr(plugin, "_periodic_sync_loop", _noop_loop)

    # Replace heavy collaborators with cheap stubs.
    import plugins.yandex_smart_home.plugin as plugin_mod

    class _Stub:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(plugin_mod, "DeviceSync", _Stub)
    monkeypatch.setattr(plugin_mod, "DeviceStatusChecker", _Stub)
    monkeypatch.setattr(plugin_mod, "YandexQuasarWS", _Stub)
    monkeypatch.setattr(plugin_mod, "CommandHandler", _Stub)

    await plugin.on_load()
    await asyncio.sleep(0)

    assert "yandex.sync_devices" in runtime.registered_services
    assert "yandex.check_devices_online" in runtime.registered_services
    assert "yandex.subscribe_device_updates" in runtime.registered_services

