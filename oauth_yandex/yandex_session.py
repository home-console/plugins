"""
YandexSession - класс для авторизации в Яндексе через различные методы.

Поддерживает:
- QR-код авторизацию
- Авторизацию через cookies
- Авторизацию через username/password
- Валидацию токенов
- Обновление cookies

Основано на YandexStation/custom_components/yandex_station/core/yandex_session.py
"""

import asyncio
import base64
import json
import logging
import re
import time
from typing import Optional, Dict, Any

from aiohttp import ClientSession
from yarl import URL

_LOGGER = logging.getLogger(__name__)

_COOKIE_B64_JSON_MAGIC = "hcj1:"


def _encode_cookie_state(session: ClientSession) -> str:
    """
    Security: do NOT use pickle for cookie persistence.
    We store a minimal JSON snapshot and base64 it.
    """
    items: list[dict[str, Any]] = []
    for c in session.cookie_jar:
        try:
            items.append(
                {
                    "name": c.key,
                    "value": c.value,
                    "domain": c.get("domain"),
                    "path": c.get("path") or "/",
                    "secure": bool(c.get("secure")),
                }
            )
        except Exception:
            continue
    raw = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _COOKIE_B64_JSON_MAGIC + base64.b64encode(raw).decode("ascii")


def _try_restore_cookie_state(session: ClientSession, cookie: str) -> None:
    """
    Accepts only JSON-based format: "hcj1:" + base64(json).
    Legacy pickle-based blobs are intentionally rejected to avoid RCE.
    """
    if not cookie:
        return
    if not cookie.startswith(_COOKIE_B64_JSON_MAGIC):
        return
    b64 = cookie[len(_COOKIE_B64_JSON_MAGIC) :]
    raw = base64.b64decode(b64)
    items = json.loads(raw.decode("utf-8"))
    if not isinstance(items, list):
        return
    session.cookie_jar.clear()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        value = it.get("value")
        domain = it.get("domain") or "yandex.ru"
        path = it.get("path") or "/"
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        # update_cookies expects a response URL context
        try:
            session.cookie_jar.update_cookies({name: value}, response_url=URL(f"https://{domain}{path}"))
        except Exception:
            continue


class LoginResponse:
    """Ответ от Yandex при авторизации."""
    
    def __init__(self, resp: dict):
        self.raw = resp

    @property
    def ok(self) -> bool:
        return self.raw.get("status") == "ok"

    @property
    def errors(self) -> list:
        return self.raw.get("errors", [])

    @property
    def error(self) -> str:
        return self.raw["errors"][0] if self.raw.get("errors") else ""

    @property
    def display_login(self) -> Optional[str]:
        return self.raw.get("display_login")

    @property
    def x_token(self) -> Optional[str]:
        return self.raw.get("x_token")

    @property
    def magic_link_email(self) -> Optional[str]:
        return self.raw.get("magic_link_email")

    @property
    def error_captcha_required(self) -> bool:
        return "captcha.required" in self.errors


class YandexSession:
    """Класс для авторизации в Яндексе через различные методы."""
    
    def __init__(
        self,
        session: ClientSession,
        x_token: Optional[str] = None,
        music_token: Optional[str] = None,
        cookie: Optional[str] = None,
    ):
        """
        :param session: aiohttp ClientSession
        :param x_token: опциональный x-token
        :param music_token: опциональный токен для glagol API
        :param cookie: опциональный base64 cookie из предыдущей сессии
        """
        self._session = session
        
        # Исправление бага с неправильным CSRF token
        setattr(session.cookie_jar, "_quote_cookie", False)
        
        self.x_token = x_token
        self.music_token = music_token
        self.auth_payload: Optional[dict] = None
        self.csrf_token: Optional[str] = None
        self.last_ts: float = 0
        
        try:
            _try_restore_cookie_state(session, cookie or "")
        except Exception:
            # best-effort восстановление cookies; на ошибке просто стартуем с пустым jar
            _LOGGER.debug("Cookie restore failed (ignored)", exc_info=True)

    async def login_username(self, username: str) -> LoginResponse:
        """Создать сессию логина и вернуть поддерживаемые методы авторизации."""
        # Шаг 1: получение csrf_token
        r = await self._get("https://passport.yandex.ru/am?app_platform=android")
        resp = await r.text()
        m = re.search(r'"csrf_token" value="([^"]+)"', resp)
        if not m:
            raise ValueError(f"Не удалось найти csrf_token в ответе: {resp[:200]}")
        self.auth_payload = {"csrf_token": m[1]}

        # Шаг 2: получение track_id
        r = await self._post(
            "https://passport.yandex.ru/registration-validations/auth/multi_step/start",
            data={**self.auth_payload, "login": username},
        )
        resp = await r.json()
        if resp.get("can_register") is True:
            return LoginResponse({"errors": ["account.not_found"]})

        if resp.get("can_authorize") is not True:
            return LoginResponse({"errors": ["authorization.failed"]})

        self.auth_payload["track_id"] = resp["track_id"]
        return LoginResponse(resp)

    async def login_password(self, password: str) -> LoginResponse:
        """Авторизация через пароль или ключ-приложение (30-секундный пароль)."""
        if not self.auth_payload:
            raise ValueError("Сначала вызовите login_username")
        
        # Шаг 3: пароль или 30-секундный ключ
        r = await self._post(
            "https://passport.yandex.ru/registration-validations/auth/multi_step/commit_password",
            data={
                **self.auth_payload,
                "password": password,
                "retpath": "https://passport.yandex.ru/am/finish?status=ok&from=Login",
            },
        )
        resp = await r.json()
        if resp["status"] != "ok":
            return LoginResponse(resp)

        if "redirect_url" in resp:
            return LoginResponse({"errors": ["redirect.unsupported"]})

        # Шаг 4: получение x_token
        return await self.login_cookies()

    async def get_qr(self) -> str:
        """Получить ссылку на QR-код для авторизации."""
        # Шаг 1: получение csrf_token
        r = await self._get("https://passport.yandex.ru/am?app_platform=android")
        resp = await r.text()
        m = re.search(r'"csrf_token" value="([^"]+)"', resp)
        if not m:
            raise ValueError(f"Не удалось найти csrf_token: {resp[:200]}")

        # Шаг 2: получение track_id
        r = await self._post(
            "https://passport.yandex.ru/registration-validations/auth/password/submit",
            data={
                "csrf_token": m[1],
                "retpath": "https://passport.yandex.ru/profile",
                "with_code": 1,
            },
        )
        resp = await r.json()
        if resp["status"] != "ok":
            raise ValueError(f"Ошибка получения QR: {resp}")

        self.auth_payload = {
            "csrf_token": resp["csrf_token"],
            "track_id": resp["track_id"],
        }

        return f"https://passport.yandex.ru/auth/magic/code/?track_id={resp['track_id']}"

    async def login_qr(self) -> LoginResponse:
        """Проверить статус QR-авторизации."""
        if not self.auth_payload:
            raise ValueError("Сначала вызовите get_qr")
        
        r = await self._post(
            "https://passport.yandex.ru/auth/new/magic/status/",
            data=self.auth_payload
        )
        resp = await r.json()
        # resp={} если авторизация еще не завершена
        if resp.get("status") != "ok":
            return LoginResponse({})

        return await self.login_cookies()

    async def login_cookies(self, cookies: Optional[str] = None) -> LoginResponse:
        """Авторизация через cookies.
        
        Поддерживает три формата:
        1. Пусто - cookies будут загружены из сессии
        2. JSON из Copy Cookies (расширение Chrome)
        3. Сырая строка cookies `key1=value1; key2=value2`
        """
        host = "passport.yandex.ru"
        if cookies is None:
            cookies = "; ".join(
                [
                    f"{c.key}={c.value}"
                    for c in self._session.cookie_jar
                    if c["domain"].endswith("yandex.ru")
                ]
            )
        elif cookies.startswith("["):
            # JSON формат
            raw = json.loads(cookies)
            host = next((p["domain"] for p in raw if p["domain"].startswith(".yandex.")), host)
            cookies = "; ".join([f"{p['name']}={p['value']}" for p in raw])

        import os
        client_secret = os.environ.get("YANDEX_CLIENT_SECRET")
        if not client_secret:
            raise RuntimeError("YANDEX_CLIENT_SECRET environment variable not set")
        
        r = await self._post(
            "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid",
            data={
                "client_id": "c0ebe342af7d48fbbbfcf2d2eedb8f9e",
                "client_secret": client_secret,
            },
            headers={"Ya-Client-Host": host, "Ya-Client-Cookie": cookies},
        )
        resp = await r.json()
        x_token = resp.get("access_token")
        if not x_token:
            return LoginResponse({"errors": ["token.not_found"]})

        return await self.validate_token(x_token)

    async def validate_token(self, x_token: str) -> LoginResponse:
        """Валидация токена и получение информации о пользователе."""
        r = await self._get(
            "https://mobileproxy.passport.yandex.net/1/bundle/account/short_info/?avatar_size=islands-300",
            headers={"Authorization": f"OAuth {x_token}"},
        )
        resp = await r.json()
        resp["x_token"] = x_token
        self.x_token = x_token
        return LoginResponse(resp)

    async def refresh_cookies(self) -> bool:
        """Проверка и обновление cookies при необходимости."""
        # Проверка cookies
        r = await self._get("https://yandex.ru/quasar?storage=1")
        resp = await r.json()
        if resp.get("storage", {}).get("user", {}).get("uid"):
            # Cookies в порядке
            return True

        # Обновление cookies
        if not self.x_token:
            return False
        
        ok = await self.login_token(self.x_token)
        return ok

    async def login_token(self, x_token: str) -> bool:
        """Авторизация в Яндексе через x-token."""
        payload = {"type": "x-token", "retpath": "https://www.yandex.ru"}
        headers = {"Ya-Consumer-Authorization": f"OAuth {x_token}"}
        r = await self._post(
            "https://mobileproxy.passport.yandex.net/1/bundle/auth/x_token/",
            data=payload,
            headers=headers,
        )
        resp = await r.json()
        if resp["status"] != "ok":
            _LOGGER.error(f"Ошибка авторизации с токеном: {resp}")
            return False

        host = resp["passport_host"]
        payload = {"track_id": resp["track_id"]}
        r = await self._get(
            f"{host}/auth/session/", params=payload, allow_redirects=False
        )
        if r.status != 302:
            _LOGGER.error(f"Ошибка получения сессии: {r.status}")
            return False

        return True

    def get_cookies_dict(self) -> Dict[str, str]:
        """Получить cookies в виде словаря."""
        cookies = {}
        for cookie in self._session.cookie_jar:
            if cookie["domain"].endswith("yandex.ru"):
                cookies[cookie.key] = cookie.value
        return cookies

    def get_cookies_string(self) -> str:
        """Получить cookies в виде строки."""
        return "; ".join(
            [
                f"{c.key}={c.value}"
                for c in self._session.cookie_jar
                if c["domain"].endswith("yandex.ru")
            ]
        )

    @property
    def cookie(self) -> str:
        """Получить cookies в безопасном формате для сохранения."""
        return _encode_cookie_state(self._session)

    async def _get(self, url: str, **kwargs):
        """Внутренний GET запрос."""
        kwargs.setdefault("timeout", 10.0)
        return await self._session.get(url, **kwargs)

    async def _post(self, url: str, **kwargs):
        """Внутренний POST запрос."""
        kwargs.setdefault("timeout", 10.0)
        return await self._session.post(url, **kwargs)
