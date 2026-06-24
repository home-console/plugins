from __future__ import annotations

import pytest

from sdk.testing import PluginTestRuntime


@pytest.mark.asyncio
async def test_oauth_yandex_storage_keys_roundtrip():
    """
    Мини-тест “плагин как отдельный репо”:
    проверяем, что plugin-level методы читают/пишут ожидаемые ключи в storage
    через SDK helpers (без прямого runtime.storage доступа).
    """

    from plugins.oauth_yandex.plugin import OAuthYandexPlugin

    rt = PluginTestRuntime()
    plugin = OAuthYandexPlugin(rt)

    # plain tokens (legacy path) should be readable
    await plugin.storage_set(plugin.TOKEN_NAMESPACE, plugin.TOKEN_KEY, {"access_token": "a", "refresh_token": "r"})
    tokens_raw = await plugin.storage_get(plugin.TOKEN_NAMESPACE, plugin.TOKEN_KEY)
    tokens = await plugin._decrypt_tokens(tokens_raw)
    assert isinstance(tokens, dict)
    assert tokens["access_token"] == "a"

    # delete via helper should work
    ok = await plugin.storage_delete(plugin.TOKEN_NAMESPACE, plugin.TOKEN_KEY)
    assert ok is True
    assert await plugin.storage_get(plugin.TOKEN_NAMESPACE, plugin.TOKEN_KEY) is None

