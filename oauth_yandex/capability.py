"""
Контракт capability `oauth:yandex` — OAuth для Яндекса.

Только описание контракта (понятие). Реализация резолва и DI запрещены.
Provider: плагин oauth_yandex (реализует этот контракт).
Consumers: yandex_smart_home и др. (требуют эту capability по ID, не импортируют контракт).

Операции контракта:
- get_access_token() -> str  — валидный OAuth access_token с auto-refresh
- get_status() -> dict       — статус авторизации (configured, authorized, access_token_valid, ...)
- get_cookies() -> dict | None — cookies сессии для Quasar API (если сохранены)
"""
from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

# Capability ID — единый идентификатор контракта (не имя плагина).
CAPABILITY_OAUTH_YANDEX = "oauth:yandex"


@runtime_checkable
class OAuthYandexCapabilityContract(Protocol):
    """
    Протокол операций capability oauth:yandex.

    Provider (oauth_yandex) реализует эти операции через сервисы.
    Consumer знает только capability ID и вызывает через фасад / ServiceRegistry.
    """

    async def get_access_token(self) -> str:
        """Валидный OAuth access_token для Яндекса (с авто-refresh при необходимости)."""
        ...

    async def get_status(self) -> Dict[str, Any]:
        """
        Статус авторизации.
        Ключи: configured, authorized, access_token_valid, expires_at, needs_user_action.
        """
        ...

    async def get_cookies(self) -> Dict[str, str] | None:
        """Cookies сессии для Quasar API (Session_id, yandexuid, ...) или None."""
        ...
