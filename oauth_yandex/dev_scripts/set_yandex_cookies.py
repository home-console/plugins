#!/usr/bin/env python3
"""
Утилита для установки Yandex session cookies для Quasar API.

Важно: не импортирует runtime (ядро/plugins/app) — только HTTP к уже
запущенному серверу, чтобы проходить validate_plugin_sdk_imports.

Quasar API требует cookies из активной сессии Яндекса, а не OAuth токен.

Как получить cookies:
1. Откройте https://yandex.ru в браузере
2. Войдите в свой аккаунт
3. Откройте DevTools (F12) → Application → Cookies → https://yandex.ru
4. Скопируйте значения важных cookies:
   - Session_id
   - yandexuid
   - sessionid2
   - i (опционально)
   - L (опционально)

Переменные окружения:
  HC_CORE_URL — базовый URL API (по умолчанию http://127.0.0.1:8000)
  HC_TOKEN — Bearer-токен для админских эндпоинтов (если включена авторизация)

Использование:
    HC_TOKEN=… python3 core-runtime-service/plugins/oauth_yandex/dev_scripts/set_yandex_cookies.py

Или напрямую через API:
    curl -X POST http://localhost:8000/api/v1/plugins/oauth-yandex/cookies \\
      -H "Content-Type: application/json" -H "Authorization: Bearer $HC_TOKEN" \\
      -d '{"Session_id": "...", "yandexuid": "...", "sessionid2": "..."}'
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _post_cookies(base: str, cookies: dict[str, str]) -> tuple[int, str]:
    """POST JSON к API сохранения cookies. Возвращает (HTTP status или 0 при сетевой ошибке, тело/error)."""
    url = base.rstrip("/") + "/api/v1/plugins/oauth-yandex/cookies"
    payload = json.dumps(cookies).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = (os.getenv("HC_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — dev script, fixed path
            body = resp.read().decode("utf-8")
            return int(resp.status), body
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return int(e.code), err_body or str(e.reason)
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def main() -> None:
    print("=== Установка Yandex Session Cookies для Quasar API ===\n")
    print("Quasar API (iot.quasar.yandex.ru) требует cookies из активной сессии Яндекса.")
    print("OAuth токен НЕ работает для Quasar API.\n")

    base = (os.getenv("HC_CORE_URL") or "http://127.0.0.1:8000").strip()

    print("Введите cookies из вашей активной сессии яндекса:")
    print("(Откройте DevTools в браузере → Application → Cookies → https://yandex.ru)\n")

    cookies: dict[str, str] = {}

    session_id = input("Session_id (обязательно): ").strip()
    if not session_id:
        print("Session_id обязателен!")
        sys.exit(1)
    cookies["Session_id"] = session_id

    yandexuid = input("yandexuid (обязательно): ").strip()
    if not yandexuid:
        print("yandexuid обязателен!")
        sys.exit(1)
    cookies["yandexuid"] = yandexuid

    sessionid2 = input("sessionid2 (опционально, Enter чтобы пропустить): ").strip()
    if sessionid2:
        cookies["sessionid2"] = sessionid2

    i_cookie = input("i (опционально, Enter чтобы пропустить): ").strip()
    if i_cookie:
        cookies["i"] = i_cookie

    l_cookie = input("L (опционально, Enter чтобы пропустить): ").strip()
    if l_cookie:
        cookies["L"] = l_cookie

    print(f"\nОтправляю cookies на {base}/api/v1/plugins/oauth-yandex/cookies ...")

    status, body = _post_cookies(base, cookies)
    if status == 0:
        print(f"Ошибка сети: {body}")
        sys.exit(1)
    if 200 <= status < 300:
        print("Cookies успешно сохранены.")
        print(f"Сохранённые ключи: {list(cookies.keys())}")
        if body:
            print(body)
        return

    print(f"Ошибка HTTP {status}: {body}")
    if status in (401, 403):
        print("Подсказка: задай HC_TOKEN (Bearer) для админского эндпоинта.")
    sys.exit(1)


if __name__ == "__main__":
    main()
