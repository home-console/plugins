from __future__ import annotations

import pytest

from plugins.yandex_device_auth.device_session import AccountSessionStore
from sdk.testing import PluginTestRuntime


@pytest.mark.asyncio
async def test_account_session_store_roundtrip(monkeypatch) -> None:
    rt = PluginTestRuntime()
    store = AccountSessionStore(rt)

    await store.save({"uid": "1", "display_login": "u"})
    loaded = await store.load()
    assert isinstance(loaded, dict)
    assert loaded["uid"] == "1"
    assert loaded["display_login"] == "u"
    assert "saved_at" in loaded

    await store.clear()
    assert await store.load() is None

