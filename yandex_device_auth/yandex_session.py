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
import os
import re
import time
from typing import Optional, Dict, Any

from aiohttp import ClientSession
from yarl import URL

_LOGGER = logging.getLogger(__name__)

_PASSPORT_URL = "https://passport.yandex.ru"
_AM_URL = f"{_PASSPORT_URL}/pwl-yandex/am?app_platform=android"
_BFF_REFERER = f"{_PASSPORT_URL}/pwl-yandex"
_SUBMIT_URL = f"{_PASSPORT_URL}/pwl-yandex/api/passport/auth/password/submit"
_MAGIC_CODE_URL = f"{_PASSPORT_URL}/pwl-yandex/api/passport/auth/magic/code"
_STATUS_URL = f"{_PASSPORT_URL}/pwl-yandex/api/passport/auth/magic/code/status"
_GET_SESSION_URL = f"{_PASSPORT_URL}/pwl-yandex/api/passport/sessions/get_session"
_QR_STATE_CONFIRMED = "otp_auth_finished"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
}

_CSRF_PATTERNS = (
    re.compile(r'"csrf_token"\s*value="([^"]+)"'),
    re.compile(r'"csrf_token"\s*:\s*"([^"]+)"'),
    re.compile(r'(?:window\.)?__CSRF__\s*=\s*"([^"]+)"'),
)

_COOKIE_B64_JSON_MAGIC = "hcj1:"


def _extract_csrf(html: str) -> str:
    for pattern in _CSRF_PATTERNS:
        match = pattern.search(html)
        if match and match.group(1):
            return match.group(1)
    raise ValueError(f"Не удалось найти csrf_token: {html[:200]}")


def _encode_cookie_state(session: ClientSession) -> str:
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
            _LOGGER.debug("Cookie restore failed (ignored)", exc_info=True)

    async def _fetch_page_csrf(self) -> str:
        r = await self._get(_AM_URL, headers=_DEFAULT_HEADERS)
        return _extract_csrf(await r.text())

    def _bff_headers(self, csrf: str) -> Dict[str, str]:
        return {
            **_DEFAULT_HEADERS,
            "X-CSRF-Token": csrf,
            "Origin": _PASSPORT_URL,
            "Referer": _BFF_REFERER,
        }

    async def login_username(self, username: str) -> LoginResponse:
        """Создать сессию логина и вернуть поддерживаемые методы авторизации."""
        csrf = await self._fetch_page_csrf()
        self.auth_payload = {"csrf_token": csrf}

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

    async def get_captcha(self) -> str:
        """Получить ссылку на изображение капчи."""
        if not self.auth_payload:
            raise ValueError("Сначала вызовите login_username")
        
        r = await self._post(
            "https://passport.yandex.ru/registration-validations/textcaptcha",
            data=self.auth_payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        resp = await r.json()
        if resp["status"] != "ok":
            raise ValueError(f"Ошибка получения капчи: {resp}")
        
        self.auth_payload["key"] = resp["key"]
        return resp["image_url"]

    async def login_captcha(self, captcha_answer: str) -> bool:
        """Авторизация с ответом на капчу."""
        if not self.auth_payload:
            raise ValueError("Сначала вызовите login_username и get_captcha")
        
        r = await self._post(
            "https://passport.yandex.ru/registration-validations/checkHuman",
            data={**self.auth_payload, "answer": captcha_answer},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        resp = await r.json()
        return resp["status"] == "ok"

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

    async def get_qr(self) -> Dict[str, Any]:
        """Получить данные для QR-кода авторизации.
        
        Returns:
            {
                "qr_url": str,  # URL для открытия в браузере
                "qr_data": str,  # Данные для генерации QR-кода (может отличаться от URL)
                "qr_svg": str,  # SVG QR-код, извлеченный со страницы Яндекса
                "track_id": str,  # track_id для проверки статуса
            }
        """
        page_csrf = await self._fetch_page_csrf()
        bff_headers = self._bff_headers(page_csrf)

        r = await self._post(
            _SUBMIT_URL,
            json={"retpath": f"{_PASSPORT_URL}/profile"},
            headers={**bff_headers, "Content-Type": "application/json"},
        )
        auth_state = await r.json()
        track_id = auth_state.get("track_id")
        if not track_id:
            raise ValueError(f"Ошибка получения QR: {auth_state}")

        r = await self._post(
            _MAGIC_CODE_URL,
            data={
                "location_id": "0",
                "magic_track_id": track_id,
                "track_id": "",
            },
            headers=bff_headers,
        )
        magic_resp = await r.json()
        qr_url = magic_resp.get("link")
        if not qr_url:
            raise ValueError(f"Ошибка получения QR link: {magic_resp}")

        self.auth_payload = {
            "page_csrf": page_csrf,
            "auth_state": auth_state,
            "track_id": track_id,
        }

        qr_data = f"yandexauth://magic?track_id={track_id}"
        
        # Извлекаем SVG QR-код со страницы Яндекса
        qr_svg = None
        try:
            # Загружаем страницу с QR-кодом
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            qr_page = await self._get(qr_url, headers=headers)
            html = await qr_page.text()
            
            # Ищем SVG QR-код в HTML
            # Яндекс использует SVG для QR-кода, обычно он находится в определенном контейнере
            # Пробуем несколько вариантов поиска
            
            # Вариант 1: Ищем SVG с viewBox (обычно QR-код имеет viewBox="0 0 53 53" или похожий)
            svg_match = re.search(r'<svg[^>]*viewBox[^>]*>.*?</svg>', html, re.DOTALL | re.IGNORECASE)
            if svg_match:
                qr_svg = svg_match.group(0)
            else:
                # Вариант 2: Ищем любой SVG с xmlns
                svg_match = re.search(r'<svg[^>]*xmlns[^>]*>.*?</svg>', html, re.DOTALL | re.IGNORECASE)
                if svg_match:
                    qr_svg = svg_match.group(0)
                else:
                    # Вариант 3: Ищем просто любой SVG
                    svg_match = re.search(r'<svg[^>]*>.*?</svg>', html, re.DOTALL)
                    if svg_match:
                        qr_svg = svg_match.group(0)
            
            if qr_svg:
                # Очищаем от лишних пробелов и переносов строк, но сохраняем структуру
                # Убираем только множественные пробелы внутри тегов
                qr_svg = re.sub(r'\s+', ' ', qr_svg)
                # Убираем пробелы между тегами
                qr_svg = re.sub(r'>\s+<', '><', qr_svg)
                qr_svg = qr_svg.strip()
                _LOGGER.debug(f"✓ SVG QR-код извлечен, размер: {len(qr_svg)} символов")
            else:
                _LOGGER.warning("SVG QR-код не найден на странице Яндекса")
        except Exception as e:
            _LOGGER.warning(f"Не удалось извлечь SVG QR-код: {e}")
            # Продолжаем без SVG - фронтенд сгенерирует QR из qr_data
        
        return {
            "qr_url": qr_url,
            "qr_data": qr_data,
            "qr_svg": qr_svg,  # SVG QR-код со страницы Яндекса
            "track_id": track_id,
        }

    async def login_qr(self) -> LoginResponse:
        """Проверить статус QR-авторизации."""
        if not self.auth_payload:
            raise ValueError("Сначала вызовите get_qr")

        page_csrf = self.auth_payload.get("page_csrf") or self.auth_payload.get("csrf_token")
        auth_state = self.auth_payload.get("auth_state")
        if not page_csrf or not auth_state:
            raise ValueError("QR-сессия устарела, начните авторизацию заново")

        headers = self._bff_headers(page_csrf)
        r = await self._post(
            _STATUS_URL,
            json=auth_state,
            headers={**headers, "Content-Type": "application/json"},
        )
        status_resp = await r.json()
        if status_resp.get("state") != _QR_STATE_CONFIRMED:
            return LoginResponse({})

        session_track_id = status_resp.get("trackId")
        if not session_track_id:
            raise ValueError(f"QR подтверждён, но trackId отсутствует: {status_resp}")

        await self._post(
            _GET_SESSION_URL,
            data={"track_id": session_track_id},
            headers=headers,
        )
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

        client_secret = (os.environ.get("YANDEX_CLIENT_SECRET") or "").strip()
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
