"""
oauth_dropbox — OAuth 2.0 PKCE провайдер для Dropbox.

Реализует capability `oauth:dropbox`. Используется cloud_sync плагином
вместо статического CLOUD_SYNC_DROPBOX_TOKEN.

Конфиг через env:
  DROPBOX_APP_KEY     — App Key из Dropbox App Console (обязательно)
  DROPBOX_APP_SECRET  — App Secret (опционально, нужен для confidential flow)
  DROPBOX_REDIRECT_URI — Redirect URI (должен совпадать с App Console)

OAuth flow (PKCE):
  1. `oauth_dropbox.get_authorize_url()` → URL для перенаправления пользователя
  2. Пользователь авторизуется, Dropbox редиректит на redirect_uri с ?code=...
  3. `oauth_dropbox.exchange_code(code, state)` → сохраняет access_token + refresh_token
  4. `oauth_dropbox.get_access_token()` → возвращает валидный токен (с auto-refresh)

Dropbox token endpoint: https://api.dropboxapi.com/oauth2/token
Authorize URL:          https://www.dropbox.com/oauth2/authorize
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from sdk.plugin_ext import BasePlugin, PluginMetadata

logger = logging.getLogger(__name__)

_NS        = "oauth_dropbox"
_TOKEN_KEY = "tokens"
_STATE_KEY  = "pkce_state"

_TOKEN_ENDPOINT     = "https://api.dropboxapi.com/oauth2/token"
_AUTHORIZE_ENDPOINT = "https://www.dropbox.com/oauth2/authorize"
_REVOKE_ENDPOINT    = "https://api.dropboxapi.com/auth/token/revoke"
# Обновляем токен за 5 минут до истечения
_REFRESH_BUFFER_SEC = 300


class OAuthDropboxPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="oauth_dropbox",
        version="0.1.0",
        description="OAuth 2.0 PKCE провайдер для Dropbox (capability: oauth:dropbox)",
        capabilities_provided=["oauth:dropbox"],
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._app_key      = ""
        self._app_secret   = ""
        self._redirect_uri = ""
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_load(self) -> None:
        self._app_key      = self.get_env_config("DROPBOX_APP_KEY", "")
        self._app_secret   = self.get_env_config("DROPBOX_APP_SECRET", "")
        self._redirect_uri = self.get_env_config(
            "DROPBOX_REDIRECT_URI",
            "http://localhost:18000/api/v1/oauth/dropbox/callback",
        )
        if not self._app_key:
            logger.warning("oauth_dropbox: DROPBOX_APP_KEY не задан — OAuth недоступен")
        await self._register_services()
        await self._register_http()

    # ------------------------------------------------------------------
    # PKCE helpers

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """Генерировать code_verifier и code_challenge (S256)."""
        verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        return verifier, challenge

    # ------------------------------------------------------------------
    # Token storage (plaintext — можно добавить шифрование как в oauth_yandex)

    async def _load_tokens(self) -> dict[str, Any] | None:
        raw = await self.storage_get(_NS, _TOKEN_KEY)
        return raw if isinstance(raw, dict) else None

    async def _save_tokens(self, tokens: dict[str, Any]) -> None:
        await self.storage_set(_NS, _TOKEN_KEY, tokens)

    async def _load_pkce_state(self) -> dict[str, Any]:
        raw = await self.storage_get(_NS, _STATE_KEY)
        return raw if isinstance(raw, dict) else {}

    async def _save_pkce_state(self, state: dict[str, Any]) -> None:
        await self.storage_set(_NS, _STATE_KEY, state)

    # ------------------------------------------------------------------
    # Services

    async def _register_services(self) -> None:

        async def get_status(**_: Any) -> dict[str, Any]:
            configured = bool(self._app_key)
            tokens = await self._load_tokens()
            authorized = tokens is not None and bool(tokens.get("access_token"))
            expires_at = tokens.get("expires_at") if tokens else None
            token_valid = False
            if expires_at:
                token_valid = float(expires_at) > (time.time() + 60)
            return {
                "ok":          True,
                "configured":  configured,
                "authorized":  authorized,
                "token_valid": token_valid,
                "expires_at":  expires_at,
                "app_key":     self._app_key[:8] + "…" if self._app_key else None,
            }

        async def get_authorize_url(**_: Any) -> dict[str, Any]:
            if not self._app_key:
                return {"ok": False, "error": "DROPBOX_APP_KEY не задан"}
            verifier, challenge = self._generate_pkce()
            state = secrets.token_urlsafe(16)
            await self._save_pkce_state({
                "verifier": verifier, "state": state, "created_at": int(time.time()),
            })
            params = {
                "client_id":             self._app_key,
                "response_type":         "code",
                "redirect_uri":          self._redirect_uri,
                "state":                 state,
                "code_challenge":        challenge,
                "code_challenge_method": "S256",
                "token_access_type":     "offline",
            }
            url = f"{_AUTHORIZE_ENDPOINT}?{urlencode(params)}"
            return {"ok": True, "url": url, "state": state}

        async def exchange_code(code: str, state: str = "", **_: Any) -> dict[str, Any]:
            if not self._app_key:
                return {"ok": False, "error": "DROPBOX_APP_KEY не задан"}
            pkce = await self._load_pkce_state()
            verifier = pkce.get("verifier", "")
            if state and pkce.get("state") != state:
                return {"ok": False, "error": "state mismatch — возможная CSRF-атака"}

            body: dict[str, str] = {
                "code":          code,
                "grant_type":    "authorization_code",
                "client_id":     self._app_key,
                "redirect_uri":  self._redirect_uri,
                "code_verifier": verifier,
            }
            if self._app_secret:
                body["client_secret"] = self._app_secret

            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.post(_TOKEN_ENDPOINT, data=body)
            if not r.is_success:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

            data = r.json()
            tokens = {
                "access_token":  data.get("access_token", ""),
                "refresh_token": data.get("refresh_token", ""),
                "expires_in":    data.get("expires_in", 14400),
                "expires_at":    time.time() + int(data.get("expires_in", 14400)),
                "account_id":    data.get("account_id", ""),
                "token_type":    data.get("token_type", "bearer"),
            }
            await self._save_tokens(tokens)
            await self._save_pkce_state({})
            logger.info("oauth_dropbox: токен получен для account_id=%s", tokens["account_id"])
            return {"ok": True, "account_id": tokens["account_id"]}

        async def get_access_token(**_: Any) -> dict[str, Any]:
            """Вернуть валидный access_token (авто-refresh если истёк)."""
            tokens = await self._load_tokens()
            if not tokens:
                return {"ok": False, "error": "Не авторизован. Вызови get_authorize_url и exchange_code."}

            expires_at = float(tokens.get("expires_at", 0))
            if expires_at - time.time() > _REFRESH_BUFFER_SEC:
                return {"ok": True, "access_token": tokens["access_token"], "expires_at": expires_at}

            # Refresh
            async with self._refresh_lock:
                tokens = await self._load_tokens()
                if not tokens:
                    return {"ok": False, "error": "Токены удалены"}
                if float(tokens.get("expires_at", 0)) - time.time() > _REFRESH_BUFFER_SEC:
                    return {"ok": True, "access_token": tokens["access_token"]}

                refresh_token = tokens.get("refresh_token", "")
                if not refresh_token:
                    return {"ok": False, "error": "refresh_token отсутствует — требуется повторная авторизация"}

                body: dict[str, str] = {
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id":     self._app_key,
                }
                if self._app_secret:
                    body["client_secret"] = self._app_secret

                async with httpx.AsyncClient(timeout=15.0) as c:
                    r = await c.post(_TOKEN_ENDPOINT, data=body)
                if not r.is_success:
                    return {"ok": False, "error": f"Refresh failed: HTTP {r.status_code}: {r.text[:200]}"}

                data = r.json()
                tokens["access_token"] = data.get("access_token", tokens["access_token"])
                tokens["expires_in"]   = data.get("expires_in", 14400)
                tokens["expires_at"]   = time.time() + int(data.get("expires_in", 14400))
                if data.get("refresh_token"):
                    tokens["refresh_token"] = data["refresh_token"]
                await self._save_tokens(tokens)
                logger.info("oauth_dropbox: access_token обновлён")
                return {"ok": True, "access_token": tokens["access_token"], "expires_at": tokens["expires_at"]}

        async def revoke(**_: Any) -> dict[str, Any]:
            tokens = await self._load_tokens()
            if tokens and tokens.get("access_token"):
                try:
                    async with httpx.AsyncClient(timeout=10.0) as c:
                        await c.post(
                            _REVOKE_ENDPOINT,
                            headers={"Authorization": f"Bearer {tokens['access_token']}"},
                        )
                except Exception:
                    pass
            await self._save_tokens({})
            logger.info("oauth_dropbox: токен отозван")
            return {"ok": True}

        await self.register_service("oauth_dropbox.get_status",       get_status)
        await self.register_service("oauth_dropbox.get_authorize_url", get_authorize_url)
        await self.register_service("oauth_dropbox.exchange_code",    exchange_code)
        await self.register_service("oauth_dropbox.get_access_token", get_access_token)
        await self.register_service("oauth_dropbox.revoke",           revoke)

    # ------------------------------------------------------------------
    # HTTP endpoints

    async def _register_http(self) -> None:
        try:
            from sdk.http import EndpointAuthConfig, HttpEndpoint
        except ImportError:
            return

        _r = EndpointAuthConfig(required_scopes=["admin.read"])
        _w = EndpointAuthConfig(required_scopes=["admin.write"])
        b  = "/api/v1/oauth/dropbox"

        for ep in [
            HttpEndpoint(method="GET",  path=f"{b}/status",   service="oauth_dropbox.get_status",        description="Статус Dropbox OAuth",     auth_config=_r),
            HttpEndpoint(method="GET",  path=f"{b}/authorize", service="oauth_dropbox.get_authorize_url", description="Получить URL авторизации", auth_config=_w),
            HttpEndpoint(method="POST", path=f"{b}/callback",  service="oauth_dropbox.exchange_code",     description="Обменять code на токен",   auth_config=_w),
            HttpEndpoint(method="POST", path=f"{b}/revoke",    service="oauth_dropbox.revoke",            description="Отозвать токен",           auth_config=_w),
        ]:
            self.register_http_endpoint(ep)
