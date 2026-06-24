"""
Фасад доступа к capability oauth:yandex и yandex:session_cookies из плагина yandex_smart_home.

ВСЕ вызовы oauth_yandex.* и yandex_device_auth.get_session (для cookies) должны идти
через этот модуль. Цель: одна точка замены реализации при введении CapabilityRegistry.

Контракт: plugins/oauth_yandex/capability.py (oauth:yandex).
Текущая реализация: делегирование в service_registry.call("oauth_yandex.*") и
yandex_device_auth.get_session + storage для cookies.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


async def get_access_token(plugin: Any) -> str:
    """
    Получить валидный OAuth access_token для Яндекса (capability oauth:yandex).

    Сейчас делегирует в oauth_yandex.get_access_token.
    При отсутствии провайдера вызов упадёт предсказуемо.
    """
    return await plugin.call_service("oauth_yandex.get_access_token")


async def get_status(plugin: Any) -> Dict[str, Any]:
    """
    Получить статус авторизации (capability oauth:yandex).

    Сейчас делегирует в oauth_yandex.get_status.
    """
    return await plugin.call_service("oauth_yandex.get_status")


async def get_cookies(plugin: Any) -> Optional[Dict[str, str]]:
    """
    Получить cookies сессии для Quasar API (capability yandex:session_cookies).

    Приоритет: device_auth (session + storage) → oauth_yandex.get_cookies → storage yandex/cookies.
    Сейчас делегирует в yandex_device_auth.get_session и oauth_yandex.get_cookies.
    """
    # 1. device_auth: сессия + storage
    try:
        if await plugin.has_service("yandex_device_auth.get_session"):
            session = await plugin.call_service("yandex_device_auth.get_session")
            if isinstance(session, dict) and session.get("linked"):
                stored = await plugin.storage_get("yandex", "cookies")
                if isinstance(stored, dict) and stored:
                    return stored
    except Exception:
        pass

    # 2. oauth_yandex.get_cookies
    try:
        if await plugin.has_service("oauth_yandex.get_cookies"):
            cookies = await plugin.call_service("oauth_yandex.get_cookies")
            if isinstance(cookies, dict) and cookies:
                return cookies
    except Exception:
        pass

    # 3. storage fallback
    try:
        stored = await plugin.storage_get("yandex", "cookies")
        if isinstance(stored, dict) and stored:
            return stored
    except Exception:
        pass

    return None
