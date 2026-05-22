"""
Device Session Models для yandex_device_auth плагина.

YandexAccountSession: состояние привязанного аккаунта (persistent).
AuthResult: результат polling.

NOTE: Old device session models removed - using simple DeviceAuthSession from yandex_passport_client.py
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class YandexAccountSession:
    """Состояние привязанного аккаунта (persistent)."""
    
    x_token: str                # OAuth token for API
    display_login: str          # Пользовательское имя
    uid: str                    # Yandex UID
    linked_at: float            # Timestamp привязки
    method: str                 # Метод авторизации ("qr" | "password" | "manual")
    device_info: Optional[Dict[str, Any]] = None  # Информация об устройстве
    
    def to_dict(self) -> Dict[str, Any]:
        """Сериализация для API ответа (без x_token)."""
        return {
            "linked": True,
            "quasar_ready": True,
            "linked_at": self.linked_at,
            "method": self.method,
            "display_login": self.display_login,
        }


@dataclass
class AuthResult:
    """Результат polling device-сессии."""
    
    state: str
    x_token: Optional[str] = None
    error: Optional[str] = None


class AccountSessionStore:
    """Хранилище account session в storage (через SDK helpers)."""
    
    def __init__(self, plugin: Any):
        self.plugin = plugin
    
    async def save(self, account_data: Dict[str, Any]):
        """Сохранить account session."""
        await self.plugin.storage_set(
            "yandex", "device_auth/session",
            {**account_data, "saved_at": time.time()}
        )
    
    async def load(self) -> Optional[Dict[str, Any]]:
        """Загрузить account session."""
        try:
            return await self.plugin.storage_get("yandex", "device_auth/session")
        except Exception:
            return None
    
    async def clear(self):
        """Удалить account session."""
        try:
            await self.plugin.storage_delete("yandex", "device_auth/session")
        except Exception:
            pass