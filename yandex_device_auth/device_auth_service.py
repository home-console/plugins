"""
YandexDeviceAuthService — Yandex Passport auth manager на основе YandexSession.

Supports:
- QR code auth (magic_x_token) — user scans QR and confirms on phone
- Password login (username + password)
- Manual cookies submission
- Token validation
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import aiohttp

from .yandex_session import YandexSession, LoginResponse

logger = logging.getLogger("yandex_device_auth")


class YandexDeviceAuthService:
    """Yandex Passport authentication manager на основе YandexSession."""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._yandex_session: Optional[YandexSession] = None
        self._auth_method: Optional[str] = None  # "qr", "cookies", "password"

    async def start_auth(
        self, method: str = "qr", options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Start Yandex authorization via QR, cookies, or username/password.
        
        Args:
            method: "qr", "cookies", "password", "token"
            options: дополнительные параметры (username, password, cookies, token)
        
        Returns:
            {
                "qr_url": str (для method="qr"),
                "oauth_url": str (alias для qr_url),
                "status": str,
                "method": str,
            }
        """
        try:
            # Создаем новую сессию
            session = aiohttp.ClientSession()
            self._yandex_session = YandexSession(session)
            self._auth_method = method

            if method == "qr":
                # QR авторизация
                qr_data = await self._yandex_session.get_qr()
                await self._log("info", "✓ QR data generated", method="qr", track_id=qr_data.get("track_id"), has_svg=bool(qr_data.get("qr_svg")))
                
                return {
                    "qr_url": qr_data.get("qr_url"),  # URL для открытия в браузере
                    "qr_data": qr_data.get("qr_data"),  # Данные для генерации QR-кода (yandexauth://...)
                    "qr_svg": qr_data.get("qr_svg"),  # SVG QR-код со страницы Яндекса
                    "oauth_url": qr_data.get("qr_url"),  # Alias для совместимости
                    "track_id": qr_data.get("track_id"),  # Для отладки
                    "status": "pending",
                    "method": "qr",
                }
            
            elif method == "cookies":
                # Авторизация через cookies
                if not options or "cookies" not in options:
                    raise ValueError("cookies required for cookies method")
                
                cookies = options["cookies"]
                resp = await self._yandex_session.login_cookies(cookies)
                
                if resp.ok:
                    await self._save_account_session(
                        {
                            "x_token": resp.x_token,
                            "display_login": resp.display_login,
                            "linked_at": time.time(),
                            "method": "cookies",
                            "cookies": self._yandex_session.get_cookies_dict(),
                        }
                    )
                    await self._publish_linked_event("cookies")
                    await self._yandex_session._session.close()
                    self._yandex_session = None
                    
                    return {
                        "status": "approved",
                        "quasar_ready": True,
                        "x_token": resp.x_token,
                        "method": "cookies",
                    }
                else:
                    raise ValueError(f"Cookies auth failed: {resp.error}")
            
            elif method == "password":
                # Авторизация через username/password
                if not options or "username" not in options:
                    raise ValueError("username required for password method")
                
                username = options["username"]
                resp = await self._yandex_session.login_username(username)
                
                if not resp.ok:
                    if resp.error_captcha_required:
                        captcha_url = await self._yandex_session.get_captcha()
                        return {
                            "status": "captcha_required",
                            "captcha_url": captcha_url,
                            "method": "password",
                        }
                    raise ValueError(f"Username login failed: {resp.error}")
                
                # Если требуется пароль, возвращаем статус
                if "password" not in options:
                    return {
                        "status": "password_required",
                        "method": "password",
                    }
                
                # Продолжаем с паролем
                password = options["password"]
                resp = await self._yandex_session.login_password(password)
                
                if resp.ok:
                    await self._save_account_session(
                        {
                            "x_token": resp.x_token,
                            "display_login": resp.display_login,
                            "linked_at": time.time(),
                            "method": "password",
                            "cookies": self._yandex_session.get_cookies_dict(),
                        }
                    )
                    await self._publish_linked_event("password")
                    await self._yandex_session._session.close()
                    self._yandex_session = None
                    
                    return {
                        "status": "approved",
                        "quasar_ready": True,
                        "x_token": resp.x_token,
                        "method": "password",
                    }
                else:
                    raise ValueError(f"Password login failed: {resp.error}")
            
            elif method == "token":
                # Валидация существующего токена
                if not options or "token" not in options:
                    raise ValueError("token required for token method")
                
                token = options["token"]
                resp = await self._yandex_session.validate_token(token)
                
                if resp.ok:
                    await self._save_account_session(
                        {
                            "x_token": resp.x_token,
                            "display_login": resp.display_login,
                            "linked_at": time.time(),
                            "method": "token",
                        }
                    )
                    await self._publish_linked_event("token")
                    await self._yandex_session._session.close()
                    self._yandex_session = None
                    
                    return {
                        "status": "approved",
                        "quasar_ready": True,
                        "x_token": resp.x_token,
                        "method": "token",
                    }
                else:
                    raise ValueError(f"Token validation failed: {resp.error}")
            
            else:
                raise ValueError(f"Unsupported auth method: {method}")

        except Exception as e:
            await self._log("error", f"Failed to start auth: {e}", method=method)
            if self._yandex_session and self._yandex_session._session:
                await self._yandex_session._session.close()
            self._yandex_session = None
            raise

    async def check_qr_status(self) -> Optional[Dict[str, Any]]:
        """Check if user confirmed QR auth and complete login flow.
        
        Returns:
            {"status": "approved", "quasar_ready": true, "x_token": str} if confirmed
            {"status": "pending"} if not confirmed yet
        """
        if not self._yandex_session or self._auth_method != "qr":
            await self._log("error", "No active QR auth session, call start_auth with method='qr' first")
            return {"status": "error", "message": "No active QR auth session"}

        try:
            # Проверяем статус QR авторизации
            resp = await self._yandex_session.login_qr()
            
            if resp.ok:
                # QR подтвержден! Сохраняем сессию
                await self._save_account_session(
                    {
                        "x_token": resp.x_token,
                        "display_login": resp.display_login,
                        "linked_at": time.time(),
                        "method": "qr",
                        "cookies": self._yandex_session.get_cookies_dict(),
                    }
                )
                await self._publish_linked_event("qr")
                logger.info(f"[Yandex] QR login successful: {resp.display_login}")
                
                # Закрываем сессию после успешного логина
                await self._yandex_session._session.close()
                self._yandex_session = None
                self._auth_method = None
                
                return {
                    "status": "approved",
                    "quasar_ready": True,
                    "x_token": resp.x_token,
                }
            
            # Еще не подтвержден
            return {"status": "pending"}
            
        except Exception as e:
            await self._log("error", f"Failed to check QR status: {e}")
            # Do not pretend "pending" when Yandex endpoints are failing (504, bad JSON, etc.).
            # Frontend needs a real error to show actionable feedback.
            return {"status": "error", "message": str(e)}

    async def get_account_status(self) -> Dict[str, Any]:
        """Get account status."""
        account = await self._load_account_session()
        if account:
            return {
                "linked": True,
                "quasar_ready": True,
                "linked_at": account.get("linked_at"),
                "method": account.get("method"),
                "display_login": account.get("display_login"),
            }
        return {"linked": False}

    async def save_cookies(
        self, cookies_json: str, method: str = "manual"
    ) -> Dict[str, Any]:
        """Save cookies from manual submission.
        
        Args:
            cookies_json: JSON array или raw cookie string
            method: auth method label
        
        Returns:
            {"status": "linked", "quasar_ready": true}
        """
        try:
            # Создаем сессию для авторизации через cookies
            session = aiohttp.ClientSession()
            yandex_session = YandexSession(session)
            
            resp = await yandex_session.login_cookies(cookies_json)
            
            if resp.ok:
                await self._save_account_session(
                    {
                        "x_token": resp.x_token,
                        "display_login": resp.display_login,
                        "linked_at": time.time(),
                        "method": method,
                        "cookies": yandex_session.get_cookies_dict(),
                    }
                )
                await self._publish_linked_event(method)
                await session.close()
                
                return {
                    "status": "linked",
                    "quasar_ready": True,
                    "x_token": resp.x_token,
                }
            else:
                await session.close()
                raise ValueError(f"Cookies auth failed: {resp.error}")
                
        except Exception as e:
            await self._log("error", f"Failed to save cookies: {e}")
            raise

    async def refresh_quasar_cookies(self) -> Dict[str, Any]:
        """Обновить cookies для Quasar API через сохранённый x_token."""
        account = await self._load_account_session()
        x_token = (account or {}).get("x_token") if isinstance(account, dict) else None
        if not x_token:
            return {"ok": False, "error": "not_linked"}

        session = aiohttp.ClientSession()
        try:
            ys = YandexSession(session, x_token=str(x_token))
            ok = await ys.login_token(str(x_token))
            if not ok:
                return {"ok": False, "error": "login_token_failed"}

            cookies = ys.get_cookies_dict()
            if account:
                account = dict(account)
                account["cookies"] = cookies
                await self._save_account_session(account)
            else:
                await self.plugin.storage_set("yandex", "cookies", cookies)
            return {"ok": True, "cookies_count": len(cookies)}
        finally:
            await session.close()

    async def unlink_account(self) -> Dict[str, Any]:
        """Unlink account and clear session."""
        try:
            # Закрываем активную сессию если есть
            if self._yandex_session and self._yandex_session._session:
                await self._yandex_session._session.close()
                self._yandex_session = None
                self._auth_method = None
            
            await self.plugin.storage_delete("yandex", "device_auth/session")
            # Также удаляем cookies для совместимости
            try:
                await self.plugin.storage_delete("yandex", "cookies")
            except Exception:
                pass
        except Exception:
            pass
        try:
            await self.plugin.publish_event(
                "yandex.device_auth.unlinked", {"quasar_ready": False}
            )
        except Exception:
            pass
        await self._log("info", "Account unlinked")
        return {"status": "unlinked"}

    async def _save_account_session(self, account_data: Dict[str, Any]):
        """Save account session to storage."""
        await self.plugin.storage_set("yandex", "device_auth/session", account_data)
        
        # Также сохраняем cookies для совместимости с Quasar
        if "cookies" in account_data:
            await self.plugin.storage_set("yandex", "cookies", account_data["cookies"])

    async def _load_account_session(self) -> Optional[Dict[str, Any]]:
        """Load account session from storage."""
        try:
            return await self.plugin.storage_get("yandex", "device_auth/session")
        except Exception:
            return None

    async def _publish_linked_event(self, method: str):
        """Publish account linked event."""
        try:
            await self.plugin.publish_event(
                "yandex.device_auth.linked",
                {
                    "method": method,
                    "linked_at": time.time(),
                    "quasar_ready": True,
                },
            )
        except Exception as e:
            await self._log("error", f"Failed to publish event: {e}")

    async def _log(self, level: str, message: str, **context):
        """Log message via logger service."""
        try:
            await self.plugin.call_service(
                "logger.log",
                level=level,
                message=message,
                plugin="yandex_device_auth",
                context=context or None,
            )
        except Exception:
            pass

    async def cleanup(self):
        """Cleanup on shutdown."""
        # Закрываем активную сессию если есть
        if self._yandex_session and self._yandex_session._session:
            await self._yandex_session._session.close()
            self._yandex_session = None
            self._auth_method = None
