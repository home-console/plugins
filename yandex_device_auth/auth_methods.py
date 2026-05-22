"""
Auth Methods для yandex_device_auth плагина.

AuthMethod: абстрактный базовый класс.
QRAuthMethod: PWL QR-авторизация через реальный Yandex API.
OneTimeCodeAuthMethod: PWL через 6-значный код.
EmailLinkAuthMethod: PWL через email.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import aiohttp
import time
import json
import re

from .device_session import AuthResult, YandexDeviceSession


class AuthMethod(ABC):
    """Абстрактный метод авторизации."""
    
    def __init__(self, plugin: Any, api_client: Any):
        self.plugin = plugin
        self.api_client = api_client
    
    @abstractmethod
    async def start(self, options: Optional[Dict[str, Any]] = None, pwl_session: Any = None) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    async def poll(self, session: YandexDeviceSession, pwl_session: Any = None) -> AuthResult:
        pass
    
    async def finalize(self, result: AuthResult, method: str) -> Dict[str, Any]:
        if result.state != "approved" or not result.cookies:
            raise ValueError(f"Cannot finalize: state={result.state}, cookies={bool(result.cookies)}")
        
        required = ["Session_id", "yandexuid"]
        missing = [k for k in required if k not in result.cookies]
        if missing:
            raise ValueError(f"Missing required cookies: {missing}")
        
        return {
            "cookies": result.cookies,
            "quasar_ready": True,
            "linked_at": time.time(),
            "method": method,
        }


class QRAuthMethod(AuthMethod):
    """PWL авторизация через QR-код."""
    
    async def start(self, options: Optional[Dict[str, Any]] = None, pwl_session: Any = None) -> Dict[str, Any]:
        """Инициирует PWL сессию с QR-кодом."""
        try:
            if pwl_session and hasattr(pwl_session, 'session'):
                response = await self.api_client.pwl_init(client_session=pwl_session.session)
            else:
                response = await self.api_client.pwl_init()
            return response
        except Exception as e:
            await self._log("error", f"PWL init failed: {e}")
            raise
    
    async def poll(self, session: YandexDeviceSession, pwl_session: Any = None) -> AuthResult:
        """Проверяет статус PWL авторизации."""
        try:
            if pwl_session and hasattr(pwl_session, 'session'):
                status = await self.api_client.pwl_check(session.device_code, client_session=pwl_session.session)
            else:
                status = await self.api_client.pwl_check(session.device_code)
            
            if status["status"] == "approved":
                return AuthResult(state="approved", cookies=status.get("cookies", {}))
            elif status["status"] == "rejected":
                return AuthResult(state="rejected", error=status.get("error"))
            elif status["status"] == "expired":
                return AuthResult(state="expired")
            else:
                return AuthResult(state="pending")
        except Exception as e:
            await self._log("debug", f"PWL poll: {e}")
            return AuthResult(state="pending")
    
    async def _log(self, level: str, message: str, **ctx):
        try:
            await self.plugin.call_service(
                "logger.log",
                level=level,
                message=message,
                plugin="yandex_device_auth",
                context=ctx or None,
            )
        except Exception:
            pass


class OneTimeCodeAuthMethod(AuthMethod):
    """PWL авторизация через 6-значный код."""
    
    async def start(self, options: Optional[Dict[str, Any]] = None, pwl_session: Any = None) -> Dict[str, Any]:
        try:
            if pwl_session and hasattr(pwl_session, 'session'):
                response = await self.api_client.pwl_init(client_session=pwl_session.session)
            else:
                response = await self.api_client.pwl_init()
            return response
        except Exception as e:
            raise
    
    async def poll(self, session: YandexDeviceSession, pwl_session: Any = None) -> AuthResult:
        try:
            if pwl_session and hasattr(pwl_session, 'session'):
                status = await self.api_client.pwl_check(session.device_code, client_session=pwl_session.session)
            else:
                status = await self.api_client.pwl_check(session.device_code)
            if status["status"] == "approved":
                return AuthResult(state="approved", cookies=status.get("cookies", {}))
            return AuthResult(state="pending")
        except:
            return AuthResult(state="pending")


class EmailLinkAuthMethod(AuthMethod):
    """PWL авторизация через email."""
    
    async def start(self, options: Optional[Dict[str, Any]] = None, pwl_session: Any = None) -> Dict[str, Any]:
        try:
            if pwl_session and hasattr(pwl_session, 'session'):
                response = await self.api_client.pwl_init(client_session=pwl_session.session)
            else:
                response = await self.api_client.pwl_init()
            return response
        except Exception as e:
            raise
    
    async def poll(self, session: YandexDeviceSession, pwl_session: Any = None) -> AuthResult:
        try:
            if pwl_session and hasattr(pwl_session, 'session'):
                status = await self.api_client.pwl_check(session.device_code, client_session=pwl_session.session)
            else:
                status = await self.api_client.pwl_check(session.device_code)
            if status["status"] == "approved":
                return AuthResult(state="approved", cookies=status.get("cookies", {}))
            return AuthResult(state="pending")
        except:
            return AuthResult(state="pending")


