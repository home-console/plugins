"""
Модуль для работы с Яндекс API.

Обеспечивает:
- Получение токенов через capability oauth:yandex (фасад oauth_provider)
- Выполнение HTTP запросов к Яндекс API
- Обработку ошибок авторизации
- Retry механизм для временных ошибок
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional
import aiohttp
import asyncio
import random

from .oauth_provider import get_access_token as oauth_get_access_token, get_cookies as oauth_get_cookies


class YandexAPIClient:
    """Клиент для работы с Яндекс IoT API."""

    BASE_URL = "https://api.iot.yandex.net/v1.0"
    
    # Параметры retry
    MAX_ATTEMPTS = 3
    BASE_DELAY = 0.5  # секунды
    JITTER_RANGE = 0.1  # ±100мс

    def __init__(self, plugin: Any, plugin_name: str):
        """Инициализация клиента.

        Args:
            plugin: SDK-first facade (BasePlugin)
            plugin_name: имя плагина для логирования
        """
        self.plugin = plugin
        self.plugin_name = plugin_name
        self._token_refresh_attempted = False  # Флаг для однократного refresh при 401

    async def get_access_token(self) -> str:
        """Получить валидный access token через capability oauth:yandex (фасад oauth_provider).

        Returns:
            Access token для использования в запросах

        Raises:
            RuntimeError: если токены недоступны, авторизация не пройдена или требуется повторная авторизация
        """
        try:
            access_token = await oauth_get_access_token(self.plugin)
            if not access_token or not isinstance(access_token, str):
                try:
                    await self.plugin.call_service(
                        "logger.log",
                        level="error",
                        message="Получен пустой или невалидный access_token от oauth:yandex",
                        plugin=self.plugin_name,
                        context={"token_type": type(access_token).__name__}
                    )
                except Exception:
                    pass
                raise RuntimeError("yandex_not_authorized")
            # Логируем успешное получение токена (первые 10 символов для безопасности)
            try:
                await self.plugin.call_service(
                    "logger.log",
                    level="debug",
                    message=f"Получен access_token: {access_token[:10]}...",
                    plugin=self.plugin_name,
                    context={}
                )
            except Exception:
                pass
            return access_token
        except RuntimeError as e:
            # Логируем ошибку получения токена
            try:
                await self.plugin.call_service(
                    "logger.log",
                    level="error",
                    message=f"Ошибка получения access_token: {str(e)}",
                    plugin=self.plugin_name,
                    context={"error": str(e), "error_type": "RuntimeError"}
                )
            except Exception:
                pass
            # Если уже RuntimeError с yandex_not_authorized - пробрасываем как есть
            error_msg = str(e)
            if error_msg == "yandex_not_authorized":
                raise
            # Проверяем, не требуется ли повторная авторизация
            if "OAuthReauthRequired" in error_msg or "требуется повторная авторизация" in error_msg.lower():
                raise RuntimeError("yandex_not_authorized")
            # Любая другая ошибка при получении токена = проблема авторизации
            raise RuntimeError("yandex_not_authorized")
        except ValueError as e:
            # ValueError от service_registry (сервис не найден) или от oauth_yandex (не настроен)
            error_msg = str(e)
            try:
                await self.plugin.call_service(
                    "logger.log",
                    level="error",
                    message=f"ValueError при получении access_token: {error_msg}",
                    plugin=self.plugin_name,
                    context={"error": error_msg, "error_type": "ValueError"}
                )
            except Exception:
                pass
            if "не найден" in error_msg.lower() or "не настроен" in error_msg.lower():
                raise RuntimeError("yandex_not_authorized")
            raise RuntimeError("yandex_not_authorized")
        except Exception as e:
            # Любая другая ошибка при получении токена = проблема авторизации
            error_msg = str(e)
            error_type = type(e).__name__
            try:
                await self.plugin.call_service(
                    "logger.log",
                    level="error",
                    message=f"Исключение при получении access_token: {error_type}: {error_msg}",
                    plugin=self.plugin_name,
                    context={"error": error_msg, "error_type": error_type}
                )
            except Exception:
                pass
            # Проверяем, не требуется ли повторная авторизация
            if "OAuthReauthRequired" in error_msg or "требуется повторная авторизация" in error_msg.lower():
                raise RuntimeError("yandex_not_authorized")
            # Все остальные ошибки тоже считаем проблемой авторизации
            raise RuntimeError("yandex_not_authorized")

    def _get_headers(self, access_token: str) -> Dict[str, str]:
        """Создать заголовки для HTTP запросов.

        Args:
            access_token: токен доступа

        Returns:
            Словарь с заголовками
        """
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _should_retry(self, status_code: Optional[int], error: Optional[Exception]) -> bool:
        """Определить, нужно ли делать retry для ошибки.
        
        Retry ТОЛЬКО для:
        - aiohttp.ClientError (сетевые ошибки)
        - timeout
        - HTTP 429 (Too Many Requests)
        - HTTP 500-599 (серверные ошибки)
        
        НЕ retry для:
        - HTTP 401, 403, 400
        - другие логические ошибки
        
        Args:
            status_code: HTTP статус код (если есть)
            error: исключение (если есть)
            
        Returns:
            True если нужно делать retry
        """
        # Сетевые ошибки - всегда retry
        if error and isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError)):
            return True
        
        # HTTP статус коды
        if status_code is not None:
            # Retry для временных ошибок
            if status_code == 429:  # Too Many Requests
                return True
            if 500 <= status_code < 600:  # Серверные ошибки
                return True
            # НЕ retry для логических ошибок
            if status_code in (400, 401, 403, 404):
                return False
        
        return False

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_data: Optional[Dict[str, Any]] = None,
        timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=10),
        read_json: bool = False
    ) -> Any:
        """Выполнить HTTP запрос с retry механизмом.
        
        Args:
            method: HTTP метод (GET, POST, etc.)
            url: URL запроса
            headers: заголовки запроса
            json_data: JSON данные для POST запросов
            timeout: таймаут запроса
            read_json: если True, читает и возвращает JSON, иначе возвращает ClientResponse
            
        Returns:
            ClientResponse объект или Dict (если read_json=True)
            
        Raises:
            RuntimeError: при ошибках запроса или исчерпании попыток
        """
        last_error = None
        last_status = None
        # Сбрасываем флаг refresh для каждого нового запроса
        self._token_refresh_attempted = False
        
        # Пытаемся использовать обёрнутый session для логирования, если доступен
        use_logged_session = False
        try:
            if await self.plugin.has_service("request_logger.create_http_session"):
                use_logged_session = True
        except Exception:
            pass
        
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            # Логируем каждый запрос
            try:
                await self.plugin.call_service(
                    "logger.log",
                    level="info",
                    message=f"Yandex API request: {method} {url}",
                    plugin=self.plugin_name,
                    context={"attempt": attempt, "max_attempts": self.MAX_ATTEMPTS}
                )
            except Exception:
                pass
            
            try:
                # Используем обёрнутый session для логирования, если доступен
                if use_logged_session:
                    session = await self.plugin.call_service(
                        "request_logger.create_http_session",
                        source=self.plugin_name,
                        timeout=timeout
                    )
                else:
                    session = aiohttp.ClientSession(timeout=timeout)
                
                async with session:
                    if method.upper() == "GET":
                        async with await session.get(url, headers=headers, timeout=timeout) as resp:
                            # Логируем ВСЕ ответы (статус код)
                            try:
                                response_text = ""
                                try:
                                    response_text = await resp.text()
                                except Exception:
                                    pass
                                
                                if resp.status == 200:
                                    await self.plugin.call_service(
                                        "logger.log",
                                        level="info",
                                        message=f"Yandex API response: {method} {url} -> HTTP {resp.status}",
                                        plugin=self.plugin_name,
                                        context={"status_code": resp.status, "attempt": attempt}
                                    )
                                else:
                                    # Для ошибок логируем детали
                                    error_detail = response_text[:500] if response_text else "no response body"
                                    await self.plugin.call_service(
                                        "logger.log",
                                        level="error",
                                        message=f"Yandex API error: {method} {url} -> HTTP {resp.status}: {error_detail}",
                                        plugin=self.plugin_name,
                                        context={
                                            "status_code": resp.status,
                                            "attempt": attempt,
                                            "error_detail": error_detail
                                        }
                                    )
                            except Exception:
                                pass
                            
                            last_status = resp.status
                            
                            # Обработка 401 - попробовать обновить токен один раз
                            if resp.status == 401 and not self._token_refresh_attempted:
                                # Логируем проблему с авторизацией
                                try:
                                    error_text = ""
                                    try:
                                        error_text = await resp.text()
                                    except Exception:
                                        pass
                                    await self.plugin.call_service(
                                        "logger.log",
                                        level="error",
                                        message=f"Yandex API 401 Unauthorized: {method} {url} - пытаемся обновить токен. Ответ: {error_text[:200]}",
                                        plugin=self.plugin_name,
                                        context={"status_code": 401, "attempt": attempt, "error_response": error_text[:200]}
                                    )
                                except Exception:
                                    pass
                                self._token_refresh_attempted = True
                                try:
                                    # Обновляем токен
                                    try:
                                        await self.plugin.call_service(
                                            "logger.log",
                                            level="info",
                                            message="Обновление access_token после 401 ошибки",
                                            plugin=self.plugin_name,
                                            context={}
                                        )
                                    except Exception:
                                        pass
                                    new_token = await self.get_access_token()
                                    headers = self._get_headers(new_token)
                                    # Повторяем запрос один раз
                                    async with await session.get(url, headers=headers, timeout=timeout) as retry_resp:
                                        # Логируем результат повторного запроса
                                        try:
                                            retry_text = ""
                                            try:
                                                retry_text = await retry_resp.text()
                                            except Exception:
                                                pass
                                            if retry_resp.status == 200:
                                                await self.plugin.call_service(
                                                    "logger.log",
                                                    level="info",
                                                    message=f"Yandex API retry success: {method} {url} -> HTTP {retry_resp.status}",
                                                    plugin=self.plugin_name,
                                                    context={"status_code": retry_resp.status}
                                                )
                                            else:
                                                await self.plugin.call_service(
                                                    "logger.log",
                                                    level="error",
                                                    message=f"Yandex API retry failed: {method} {url} -> HTTP {retry_resp.status}: {retry_text[:200]}",
                                                    plugin=self.plugin_name,
                                                    context={"status_code": retry_resp.status, "error_response": retry_text[:200]}
                                                )
                                        except Exception:
                                            pass
                                        last_status = retry_resp.status
                                        if retry_resp.status == 200:
                                            if read_json:
                                                try:
                                                    return await retry_resp.json()
                                                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                                                    raise RuntimeError(f"Ошибка чтения ответа API: {e}")
                                                except Exception as e:
                                                    # Если не JSON, пытаемся прочитать как текст для диагностики
                                                    try:
                                                        text = await retry_resp.text()
                                                        raise RuntimeError(f"Ошибка парсинга JSON ответа API: {e} — {text[:200]}")
                                                    except (aiohttp.ClientError, asyncio.TimeoutError) as read_error:
                                                        raise RuntimeError(f"Ошибка чтения ответа API: {read_error}")
                                            return retry_resp
                                        # Если снова 401, не retry
                                        if retry_resp.status == 401:
                                            text = await retry_resp.text()
                                            raise RuntimeError(f"Ошибка Яндекс API: HTTP {retry_resp.status} — {text[:200]}")
                                        # Другие ошибки обрабатываем как обычно
                                        if not self._should_retry(retry_resp.status, None):
                                            text = await retry_resp.text()
                                            raise RuntimeError(f"Ошибка Яндекс API: HTTP {retry_resp.status} — {text[:200]}")
                                        # Продолжаем retry loop
                                        last_status = retry_resp.status
                                        continue
                                except RuntimeError:
                                    raise
                                except Exception as e:
                                    # Ошибка обновления токена
                                    raise RuntimeError(f"Ошибка обновления токена: {e}")
                            
                            # Проверяем успешный ответ
                            if resp.status == 200:
                                if read_json:
                                    try:
                                        return await resp.json()
                                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                                        raise RuntimeError(f"Ошибка чтения ответа API: {e}")
                                    except Exception as e:
                                        # Если не JSON, пытаемся прочитать как текст для диагностики
                                        try:
                                            text = await resp.text()
                                            raise RuntimeError(f"Ошибка парсинга JSON ответа API: {e} — {text[:200]}")
                                        except (aiohttp.ClientError, asyncio.TimeoutError) as read_error:
                                            raise RuntimeError(f"Ошибка чтения ответа API: {read_error}")
                                return resp
                            
                            # 404 обрабатываем отдельно (не retry, не читаем JSON)
                            if resp.status == 404:
                                raise RuntimeError(f"Ошибка Яндекс API: HTTP 404 — Not Found")
                            
                            # Проверяем, нужно ли retry
                            if not self._should_retry(resp.status, None):
                                try:
                                    text = await resp.text()
                                    raise RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status} — {text[:200]}")
                                except (aiohttp.ClientError, asyncio.TimeoutError) as read_error:
                                    # Ошибка при чтении ответа - это тоже сетевая ошибка
                                    raise RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status} — {read_error}")
                            
                            # Сохраняем для retry (читаем текст только если нужно)
                            last_status = resp.status
                            try:
                                text = await resp.text()
                                last_error = RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status} — {text[:200]}")
                            except (aiohttp.ClientError, asyncio.TimeoutError) as read_error:
                                # Ошибка при чтении ответа - это тоже сетевая ошибка, делаем retry
                                last_error = read_error
                            except Exception:
                                last_error = RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status}")
                    
                    elif method.upper() == "POST":
                        async with await session.post(url, headers=headers, json=json_data, timeout=timeout) as resp:
                            # Логируем ВСЕ ответы
                            try:
                                response_text = ""
                                try:
                                    response_text = await resp.text()
                                except Exception:
                                    pass
                                
                                if 200 <= resp.status < 300:
                                    await self.plugin.call_service(
                                        "logger.log",
                                        level="info",
                                        message=f"Yandex API response: {method} {url} -> HTTP {resp.status}",
                                        plugin=self.plugin_name,
                                        context={"status_code": resp.status, "attempt": attempt}
                                    )
                                else:
                                    error_detail = response_text[:500] if response_text else "no response body"
                                    await self.plugin.call_service(
                                        "logger.log",
                                        level="error",
                                        message=f"Yandex API error: {method} {url} -> HTTP {resp.status}: {error_detail}",
                                        plugin=self.plugin_name,
                                        context={
                                            "status_code": resp.status,
                                            "attempt": attempt,
                                            "error_detail": error_detail
                                        }
                                    )
                            except Exception:
                                pass
                            
                            last_status = resp.status
                            
                            # Обработка 401 для POST
                            if resp.status == 401 and not self._token_refresh_attempted:
                                try:
                                    error_text = ""
                                    try:
                                        error_text = await resp.text()
                                    except Exception:
                                        pass
                                    await self.plugin.call_service(
                                        "logger.log",
                                        level="error",
                                        message=f"Yandex API 401 Unauthorized: {method} {url} - пытаемся обновить токен. Ответ: {error_text[:200]}",
                                        plugin=self.plugin_name,
                                        context={"status_code": 401, "attempt": attempt, "error_response": error_text[:200]}
                                    )
                                except Exception:
                                    pass
                            
                            # Обработка 401 - попробовать обновить токен один раз
                            if resp.status == 401 and not self._token_refresh_attempted:
                                self._token_refresh_attempted = True
                                try:
                                    # Обновляем токен
                                    new_token = await self.get_access_token()
                                    headers = self._get_headers(new_token)
                                    # Повторяем запрос один раз
                                    async with await session.post(url, headers=headers, json=json_data, timeout=timeout) as retry_resp:
                                        last_status = retry_resp.status
                                        if 200 <= retry_resp.status < 300:
                                            if read_json:
                                                try:
                                                    return await retry_resp.json()
                                                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                                                    raise RuntimeError(f"Ошибка чтения ответа API: {e}")
                                                except Exception as e:
                                                    # Если не JSON, пытаемся прочитать как текст
                                                    try:
                                                        text = await retry_resp.text()
                                                        return {"status": "ok", "raw_response": text[:500]}
                                                    except Exception:
                                                        return {"status": "ok", "raw_response": "Response received but parsing failed"}
                                            return retry_resp
                                        # Если снова 401, не retry
                                        if retry_resp.status == 401:
                                            text = await retry_resp.text()
                                            raise RuntimeError(f"Ошибка Яндекс API: HTTP {retry_resp.status} — {text[:200]}")
                                        # Другие ошибки обрабатываем как обычно
                                        if not self._should_retry(retry_resp.status, None):
                                            text = await retry_resp.text()
                                            raise RuntimeError(f"Ошибка Яндекс API: HTTP {retry_resp.status} — {text[:200]}")
                                        # Продолжаем retry loop
                                        last_status = retry_resp.status
                                        continue
                                except RuntimeError:
                                    raise
                                except Exception as e:
                                    # Ошибка обновления токена
                                    raise RuntimeError(f"Ошибка обновления токена: {e}")
                            
                            # Проверяем успешный ответ
                            if 200 <= resp.status < 300:
                                if read_json:
                                    try:
                                        return await resp.json()
                                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                                        raise RuntimeError(f"Ошибка чтения ответа API: {e}")
                                    except Exception as e:
                                        # Если не JSON, пытаемся прочитать как текст
                                        try:
                                            text = await resp.text()
                                            return {"status": "ok", "raw_response": text[:500]}
                                        except Exception:
                                            return {"status": "ok", "raw_response": "Response received but parsing failed"}
                                return resp
                            
                            # Проверяем, нужно ли retry
                            if not self._should_retry(resp.status, None):
                                try:
                                    text = await resp.text()
                                    raise RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status} — {text[:200]}")
                                except (aiohttp.ClientError, asyncio.TimeoutError) as read_error:
                                    # Ошибка при чтении ответа - это тоже сетевая ошибка
                                    raise RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status} — {read_error}")
                            
                            # Сохраняем для retry (читаем текст только если нужно)
                            last_status = resp.status
                            try:
                                text = await resp.text()
                                last_error = RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status} — {text[:200]}")
                            except (aiohttp.ClientError, asyncio.TimeoutError) as read_error:
                                # Ошибка при чтении ответа - это тоже сетевая ошибка, делаем retry
                                last_error = read_error
                            except Exception:
                                last_error = RuntimeError(f"Ошибка Яндекс API: HTTP {resp.status}")
                    else:
                        raise RuntimeError(f"Неподдерживаемый HTTP метод: {method}")
            
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                # Логируем сетевую ошибку
                try:
                    await self.plugin.call_service(
                        "logger.log",
                        level="warning",
                        message=f"Yandex API network error: {method} {url} - {e}",
                        plugin=self.plugin_name,
                        context={
                            "method": method,
                            "url": url,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "attempt": attempt
                        }
                    )
                except Exception:
                    pass
                if not self._should_retry(None, e):
                    raise RuntimeError(f"Сетевая ошибка при запросе к Яндекс API: {e}")
            except Exception as e:
                # Ловим любые другие исключения (например, при чтении ответа)
                # Проверяем, является ли это сетевой ошибкой
                if isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError)):
                    last_error = e
                    # Логируем сетевую ошибку
                    try:
                        await self.plugin.call_service(
                            "logger.log",
                            level="warning",
                            message=f"Yandex API network error: {method} {url} - {e}",
                            plugin=self.plugin_name,
                            context={
                                "method": method,
                                "url": url,
                                "error": str(e),
                                "error_type": type(e).__name__,
                                "attempt": attempt
                            }
                        )
                    except Exception:
                        pass
                    if not self._should_retry(None, e):
                        raise RuntimeError(f"Сетевая ошибка при запросе к Яндекс API: {e}")
                else:
                    # Не сетевые ошибки - не retry, но логируем
                    try:
                        await self.plugin.call_service(
                            "logger.log",
                            level="error",
                            message=f"Yandex API error: {method} {url} - {e}",
                            plugin=self.plugin_name,
                            context={
                                "method": method,
                                "url": url,
                                "error": str(e),
                                "error_type": type(e).__name__,
                                "attempt": attempt
                            }
                        )
                    except Exception:
                        pass
                    raise RuntimeError(f"Ошибка при запросе к Яндекс API: {e}")
            
            # Логируем попытку retry
            if attempt < self.MAX_ATTEMPTS:
                try:
                    await self.plugin.call_service(
                        "logger.log",
                        level="warning",
                        message=f"Retry attempt {attempt}/{self.MAX_ATTEMPTS} для {method} {url}",
                        plugin=self.plugin_name,
                        context={
                            "attempt": attempt,
                            "max_attempts": self.MAX_ATTEMPTS,
                            "status_code": last_status,
                            "error_type": type(last_error).__name__ if last_error else None
                        }
                    )
                except Exception:
                    pass
                
                # Вычисляем задержку с exponential backoff и jitter
                delay = self.BASE_DELAY * (2 ** (attempt - 1))
                jitter = random.uniform(-self.JITTER_RANGE, self.JITTER_RANGE)
                total_delay = max(0, delay + jitter)
                
                await asyncio.sleep(total_delay)
        
        # Исчерпаны все попытки
        try:
            await self.plugin.call_service(
                "logger.log",
                level="error",
                message=f"Исчерпаны все попытки ({self.MAX_ATTEMPTS}) для {method} {url}",
                plugin=self.plugin_name,
                context={
                    "max_attempts": self.MAX_ATTEMPTS,
                    "status_code": last_status,
                    "error_type": type(last_error).__name__ if last_error else None
                }
            )
        except Exception:
            pass
        
        if last_error:
            if isinstance(last_error, RuntimeError):
                raise last_error
            raise RuntimeError(f"Сетевая ошибка при запросе к Яндекс API после {self.MAX_ATTEMPTS} попыток: {last_error}")
        
        raise RuntimeError(f"Ошибка Яндекс API после {self.MAX_ATTEMPTS} попыток: HTTP {last_status}")

    async def get_user_info(self) -> Dict[str, Any]:
        """Получить информацию о пользователе и устройствах.

        Returns:
            Ответ API с устройствами

        Raises:
            RuntimeError: при ошибках запроса или авторизации
        """
        try:
            import aiohttp
        except ImportError:
            raise RuntimeError("Требуется установить aiohttp для синхронизации устройств")

        access_token = await self.get_access_token()
        url = f"{self.BASE_URL}/user/info"
        headers = self._get_headers(access_token)

        return await self._request_with_retry("GET", url, headers, read_json=True)

    async def get_device_info(self, device_id: str) -> Dict[str, Any]:
        """Получить информацию об устройстве.

        Args:
            device_id: ID устройства в Яндекс API

        Returns:
            Информация об устройстве

        Raises:
            RuntimeError: при ошибках запроса
        """
        try:
            import aiohttp
        except ImportError:
            raise RuntimeError("Требуется установить aiohttp для проверки статуса устройств")

        access_token = await self.get_access_token()
        url = f"{self.BASE_URL}/devices/{device_id}"
        headers = self._get_headers(access_token)

        try:
            return await self._request_with_retry("GET", url, headers, read_json=True)
        except RuntimeError as e:
            # Проверяем, не 404 ли это
            error_msg = str(e)
            if "404" in error_msg or "HTTP 404" in error_msg or "Not Found" in error_msg:
                raise RuntimeError(f"Устройство {device_id} не найдено (404)")
            raise

    async def get_devices_list(self) -> Dict[str, Any]:
        """Получить список всех устройств.

        Returns:
            Ответ API со списком устройств

        Raises:
            RuntimeError: при ошибках запроса
        """
        try:
            import aiohttp
        except ImportError:
            raise RuntimeError("Требуется установить aiohttp для получения списка устройств")

        access_token = await self.get_access_token()
        url = f"{self.BASE_URL}/devices"
        headers = self._get_headers(access_token)

        return await self._request_with_retry("GET", url, headers, read_json=True)

    async def send_device_action(
        self, device_id: str, actions: list[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Отправить команду устройству.

        Args:
            device_id: ID устройства в Яндекс API
            actions: список действий в формате Яндекс API

        Returns:
            Ответ API

        Raises:
            RuntimeError: при ошибках запроса
        """
        try:
            import aiohttp
        except ImportError:
            raise RuntimeError("Требуется установить aiohttp для отправки команд")

        access_token = await self.get_access_token()
        url = f"{self.BASE_URL}/devices/actions"
        headers = self._get_headers(access_token)

        request_body = {
            "devices": [
                {
                    "id": device_id,
                    "actions": actions
                }
            ]
        }

        return await self._request_with_retry("POST", url, headers, json_data=request_body, read_json=True)

    def _quasar_cookie_jar(self, cookies: Dict[str, str]):
        """CookieJar для Quasar — тот же способ, что в yandex_quasar_ws (domain .yandex.ru)."""
        jar = aiohttp.CookieJar(unsafe=True)
        if not cookies:
            return jar
        from http.cookies import SimpleCookie
        from yarl import URL
        cookie_dict = SimpleCookie()
        for name, value in cookies.items():
            cookie_dict[name] = str(value)
            cookie_dict[name]["domain"] = ".yandex.ru"
            cookie_dict[name]["path"] = "/"
        base_url = URL("https://iot.quasar.yandex.ru")
        jar.update_cookies(cookie_dict, response_url=base_url)
        jar.update_cookies(cookie_dict, response_url=URL("https://yandex.ru"))
        return jar

    async def _get_quasar_csrf_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Получить CSRF-токен для POST (как в YandexStation: GET yandex.ru/quasar, парсим csrfToken2)."""
        try:
            async with session.get(
                "https://yandex.ru/quasar",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.text()
                m = re.search(r'"csrfToken2"\s*:\s*"([^"]+)"', raw)
                if m:
                    return m.group(1)
                return None
        except Exception:
            return None

    async def send_device_action_quasar(
        self, device_id: str, actions: list
    ) -> Dict[str, Any]:
        """Отправить команду устройству через Quasar API (cookies, без OAuth).

        Используется при входе через device auth, когда OAuth токена нет.
        Формат Quasar: POST /m/user/devices/{id}/actions, body {"actions": [...]}
        Cookies и заголовки — как в Quasar WS (domain .yandex.ru, User-Agent).
        """
        try:
            from yarl import URL
        except ImportError:
            raise RuntimeError("Требуется yarl для Quasar API")
        cookies = await oauth_get_cookies(self.plugin) or {}
        if not cookies:
            raise RuntimeError("Cookies required for Quasar. Use device auth or set cookies.")
        jar = self._quasar_cookie_jar(cookies)
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://iot.quasar.yandex.ru",
            "Referer": "https://iot.quasar.yandex.ru/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        # Пробуем оба пути: /m/user/ (как в YandexStation) и /m/v3/
        urls_to_try = [
            f"https://iot.quasar.yandex.ru/m/user/devices/{device_id}/actions",
            f"https://iot.quasar.yandex.ru/m/v3/user/devices/{device_id}/actions",
        ]
        # Варианты тела: часть клиентов Quasar ожидает "actions", часть — "states"; state с value без instance
        states_fallback = []
        for a in actions:
            if isinstance(a, dict):
                st = a.get("state") or {}
                states_fallback.append({
                    "type": a.get("type", "devices.capabilities.on_off"),
                    "state": {"value": st.get("value")} if "value" in st else st,
                })
        bodies_to_try = [
            {"actions": actions},
            {"states": actions},
            {"states": states_fallback} if states_fallback else None,
        ]
        timeout = aiohttp.ClientTimeout(total=15)
        last_error = None
        async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
            csrf_token = await self._get_quasar_csrf_token(session)
            if csrf_token:
                base_headers["x-csrf-token"] = csrf_token
            for request_body in bodies_to_try:
                if request_body is None:
                    continue
                for url in urls_to_try:
                    try:
                        async with session.post(url, headers=base_headers, json=request_body) as resp:
                            text = await resp.text()
                            if resp.status < 400:
                                try:
                                    await self.plugin.call_service(
                                        "logger.log",
                                        level="info",
                                        message=f"Quasar command OK for {device_id}",
                                        plugin=self.plugin_name,
                                        context={"url": url, "body_key": list(request_body.keys())[0]},
                                    )
                                except Exception:
                                    pass
                                if not text.strip():
                                    return {}
                                try:
                                    import json as _json
                                    return _json.loads(text)
                                except Exception:
                                    return {}
                            last_error = (resp.status, text)
                            try:
                                await self.plugin.call_service(
                                    "logger.log",
                                    level="warning",
                                    message=f"Quasar {resp.status} for {url}: {text[:500]}",
                                    plugin=self.plugin_name,
                                    context={"url": url, "status": resp.status, "body_preview": text[:300]},
                                )
                            except Exception:
                                pass
                    except Exception as e:
                        last_error = (0, str(e))
        status, text = last_error or (403, "Forbidden")
        raise RuntimeError(f"Quasar actions HTTP {status}: {text[:400]}")

    async def get_quasar_devices(self) -> Dict[str, Any]:
        """Получить список устройств через Quasar API (с домами и комнатами).

        Quasar API возвращает структуру с households (домами) и комнатами.
        Использует cookies сессии, а не OAuth token.

        Returns:
            Ответ Quasar API со структурой households -> rooms -> devices

        Raises:
            RuntimeError: при ошибках запроса или отсутствии cookies
        """
        try:
            import aiohttp
            from yarl import URL
        except ImportError:
            raise RuntimeError("Требуется установить aiohttp и yarl для Quasar API")

        # Capability yandex:session_cookies — единая точка через фасад oauth_provider
        cookies = await oauth_get_cookies(self.plugin) or {}

        if not cookies:
            raise RuntimeError("Cookies required for Quasar API. Configure capability yandex:session_cookies (device auth or OAuth with cookies).")

        # Создаем CookieJar
        jar = aiohttp.CookieJar()
        base_url = URL("https://iot.quasar.yandex.ru")
        for name, value in cookies.items():
            jar.update_cookies({name: value}, response_url=base_url)

        # Заголовки для Quasar API (БЕЗ Authorization!)
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://iot.quasar.yandex.ru",
        }

        url = "https://iot.quasar.yandex.ru/m/v3/user/devices"
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Quasar devices HTTP {resp.status}: {text[:500]}")

                try:
                    return await resp.json()
                except Exception as parse_err:
                    text = await resp.text()
                    raise RuntimeError(f"Quasar devices parse error: {parse_err} — {text[:200]}")
