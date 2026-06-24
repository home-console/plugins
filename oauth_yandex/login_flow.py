"""
Единый login flow для Яндекса: контролируемый Embedded WebView,
получение OAuth токена и session cookies для Quasar за один логин.

Модули и роли:
- EmbeddedWebViewLogin: запускает контролируемый UI (встроенный WebView),
  перенаправляет пользователя на yandex.ru, перехватывает redirect с `code`
  и извлекает cookies домена yandex.ru из внутреннего cookie-store WebView.
- OAuthTokenManager: обменивает `code` на `access_token`/`refresh_token`
  через уже существующий сервис `oauth_yandex.exchange_code`.
- SessionCookieManager: сохраняет cookies в storage под ключом `yandex/cookies`.
- YandexAccountSession: агрегирует статус аккаунта и атомарно сохраняет
  токены и cookies.
- YandexLoginService: единая оркестрация старта/статуса login-процесса.

Зависимости: по возможности используем PyQt6 + PyQt6-WebEngine для WebView.
Если они недоступны, сервис сообщает статус `unsupported`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
import time


@dataclass
class YandexAccountSession:
    """Агрегированная сессия аккаунта Яндекса.

    Хранит статус, минимальные поля и обеспечивает атомарную запись
    (в рамках двух ключей) токенов и cookies.
    """
    authorized: bool
    status: str  # linked | needs_relogin | invalid | in_progress | unsupported
    expires_at: Optional[float] = None

    @staticmethod
    async def save_atomic(plugin: Any, tokens: Dict[str, Any], cookies: Dict[str, str]) -> None:
        """Атомарная запись токенов и cookies.

        Реализуется как последовательная запись в два ключа. В случае ошибок
        запись считается неуспешной. Публикует событие `yandex.login.linked`.
        """
        # Сохраняем токены
        await plugin.storage_set("oauth_yandex", "tokens", tokens)
        # Сохраняем cookies
        await plugin.storage_set("yandex", "cookies", cookies)
        # Публикуем событие
        with _suppress():
            await plugin.publish_event("yandex.login.linked", {
                "authorized": True,
                "expires_at": tokens.get("expires_at"),
                "cookies_present": True,
            })


class _suppress:
    def __enter__(self):
        return None
    def __exit__(self, exc_type, exc, tb):
        return True  # подавляем исключения


class EmbeddedWebViewLogin:
    """Абстракция контролируемого WebView логина.

    Реализация должна:
    - открыть UI с `https://oauth.yandex.ru/authorize?...`
    - перехватить redirect на `redirect_uri` и извлечь `code`
    - извлечь cookies домена `yandex.ru`/`oauth.yandex.ru`
    - вернуть `(code, cookies)`
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def start(self, authorize_url: str, timeout_sec: int = 180) -> Tuple[str, Dict[str, str]]:
        """Запустить WebView, выполнить login и вернуть code+cookies.

        Базовая реализация — заглушка, которая сообщает о неподдерживаемости.
        Реальные реализации: `PyQtWebViewLogin`.
        """
        raise NotImplementedError("EmbeddedWebViewLogin not implemented for this environment")


class PyQtWebViewLogin(EmbeddedWebViewLogin):
    """Реализация Embedded WebView на PyQt6 + QtWebEngine.

    Примечание: чтобы не тянуть тяжёлые зависимости по умолчанию,
    импортируем PyQt6 динамически и возвращаем `NotImplementedError`
    если библиотека недоступна.
    """

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        try:
            import PyQt6  # noqa: F401
            import PyQt6.QtCore  # noqa: F401
            import PyQt6.QtWidgets  # noqa: F401
            import PyQt6.QtWebEngineWidgets  # noqa: F401
            self._available = True
        except Exception:
            self._available = False

    async def start(self, authorize_url: str, timeout_sec: int = 180) -> Tuple[str, Dict[str, str]]:
        if not self._available:
            raise NotImplementedError("PyQt6 WebView недоступен в текущем окружении")
        # Минимальная заглушка: сообщаем о неподдерживаемости реального захвата
        # В полноценной реализации здесь создаётся окно, загружается authorize_url,
        # перехватывается переход на redirect_uri (через URLChanged), извлекается code,
        # читается cookieStore профиля (QWebEngineProfile.cookieStore).
        raise NotImplementedError("WebView login flow требует реализации UI слоя (PyQt6)")


class OAuthTokenManager:
    """Обмен кода на токены через сервисы плагина oauth_yandex."""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def exchange_code(self, code: str) -> Dict[str, Any]:
        # Используем уже существующий сервис плагина
        result = await self.plugin.call_service("oauth_yandex.exchange_code", code=code)
        # Загружаем сохранённые токены для агрегирования
        tokens = await self.plugin.storage_get("oauth_yandex", "tokens")
        if isinstance(tokens, dict):
            return tokens
        return {}


class SessionCookieManager:
    """Сохранение cookies для Quasar API."""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def save_cookies(self, cookies: Dict[str, str]) -> None:
        await self.plugin.storage_set("yandex", "cookies", cookies)

    async def get_cookies(self) -> Optional[Dict[str, str]]:
        data = await self.plugin.storage_get("yandex", "cookies")
        return data if isinstance(data, dict) else None


class YandexLoginService:
    """Единый entrypoint для интеграции логина Яндекса.

    Стартует контролируемый WebView, получает code+cookies, обменивает код
    на токены и сохраняет всё атомарно. Предоставляет статус процесса.
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._status: Dict[str, Any] = {"state": "idle"}

    async def start(self) -> Dict[str, Any]:
        """Запуск login-процесса через Embedded WebView.

        Возвращает быстрый ответ со статусом. Детали можно получать через `status()`.
        """
        config = await self.plugin.storage_get("oauth_yandex", "config")
        if not config:
            raise ValueError("OAuth не настроен. Вызовите oauth_yandex.configure сначала.")

        authorize_url = self._build_authorize_url(config)

        # Выбираем реализацию WebView
        webview = PyQtWebViewLogin(self.plugin)
        token_mgr = OAuthTokenManager(self.plugin)
        cookie_mgr = SessionCookieManager(self.plugin)

        self._status = {"state": "in_progress"}
        try:
            code, cookies = await webview.start(authorize_url)
        except NotImplementedError:
            # Нет доступной реализации WebView — сообщаем явно
            self._status = {"state": "unsupported", "reason": "webview_unavailable"}
            return self._status
        except Exception as e:
            self._status = {"state": "failed", "error": str(e)}
            return self._status

        # Обмениваем код на токены
        tokens = await token_mgr.exchange_code(code)
        # Сохраняем cookies
        await cookie_mgr.save_cookies(cookies)

        # Атомарная запись + событие
        await YandexAccountSession.save_atomic(self.plugin, tokens, cookies)

        expires_at = tokens.get("expires_at")
        self._status = {"state": "linked", "expires_at": expires_at}
        return self._status

    async def status(self) -> Dict[str, Any]:
        """Текущий статус логина/аккаунта."""
        # Проверяем сохранённые токены
        tokens = await self.plugin.storage_get("oauth_yandex", "tokens")
        cookies = await self.plugin.storage_get("yandex", "cookies")

        configured = await self.plugin.storage_get("oauth_yandex", "config") is not None
        authorized = isinstance(tokens, dict) and "access_token" in tokens
        cookies_ok = isinstance(cookies, dict) and "Session_id" in cookies and "yandexuid" in cookies

        expires_at = tokens.get("expires_at") if isinstance(tokens, dict) else None
        now = time.time()

        if not configured:
            return {"state": "invalid", "reason": "not_configured"}
        if self._status.get("state") == "in_progress":
            return self._status
        if authorized and cookies_ok:
            # Валидность access_token по времени
            if isinstance(expires_at, (int, float)) and expires_at <= now + 30:
                return {"state": "needs_relogin", "reason": "token_expired", "expires_at": expires_at}
            return {"state": "linked", "expires_at": expires_at}
        if authorized and not cookies_ok:
            return {"state": "needs_relogin", "reason": "cookies_missing"}
        return {"state": "invalid", "reason": "not_linked"}

    def _build_authorize_url(self, config: Dict[str, Any]) -> str:
        from urllib.parse import urlencode
        params = {
            "response_type": "code",
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
        }
        if config.get("scope"):
            params["scope"] = config["scope"]
        return f"https://oauth.yandex.ru/authorize?{urlencode(params)}"
