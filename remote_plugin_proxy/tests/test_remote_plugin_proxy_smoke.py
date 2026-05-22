from __future__ import annotations

import pytest

from sdk.testing import PluginTestRuntime


def test_metadata_default_smoke() -> None:
    from plugins.remote_plugin_proxy import RemotePluginProxy

    runtime = PluginTestRuntime()
    plugin = RemotePluginProxy(runtime, remote_url="http://example.com")
    md = plugin.metadata
    assert md.name == "remote_plugin"
    assert md.version == "0.0.0"


@pytest.mark.asyncio
async def test_on_load_registers_services_and_on_unload_unregisters() -> None:
    from plugins.remote_plugin_proxy import RemotePluginProxy

    runtime = PluginTestRuntime()
    plugin = RemotePluginProxy(runtime, remote_url="http://example.com")

    async def _fake_http_call(endpoint: str, method: str = "GET", json_data=None):
        if endpoint == "/plugin/metadata":
            return {
                "name": "remote_echo",
                "version": "1.0.0",
                "description": "echo service",
                "author": "test",
                "services": [
                    {
                        "name": "remote.echo",
                        "endpoint": "/echo",
                        "method": "POST",
                    }
                ],
            }
        # plugin/load, plugin/unload etc. - non-fatal in tests
        return {"ok": True}

    plugin._http_call = _fake_http_call  # type: ignore[assignment]

    await plugin.on_load()
    assert "remote.echo" in runtime.registered_services

    await plugin.on_unload()
    assert "remote.echo" not in runtime.registered_services

