# Quasar API — Шпаргалка

## ⚠️ ВАЖНО

```
Quasar API (iot.quasar.yandex.ru)
├── ❌ НЕ использует OAuth
├── ❌ НЕ принимает Authorization: Bearer
└── ✅ Использует ТОЛЬКО cookies сессии
```

## Обязательные cookies

```json
{
  "Session_id": "...",  // ОБЯЗАТЕЛЬНО
  "yandexuid": "..."    // ОБЯЗАТЕЛЬНО
}
```

## Получить cookies

```bash
# Способ 1: Через скрипт
python3 dev-scripts/set_yandex_cookies.py

# Способ 2: Через API
curl -X POST http://localhost:8000/oauth/yandex/cookies \
  -H "Content-Type: application/json" \
  -d '{"Session_id":"...", "yandexuid":"..."}'

# Способ 3: Проверить текущие
curl http://localhost:8000/oauth/yandex/cookies
```

## Где взять cookies

1. Браузер → https://yandex.ru (войти в аккаунт)
2. F12 → Application → Cookies → https://yandex.ru
3. Скопировать `Session_id` и `yandexuid`

## Код

### ✅ Правильно

```python
# Загрузить cookies
cookies = await self.call_service("oauth_yandex.get_cookies")

# Создать CookieJar
jar = aiohttp.CookieJar()
for name, value in cookies.items():
    jar.update_cookies({name: value}, 
                      response_url="https://iot.quasar.yandex.ru")

# Запрос БЕЗ Authorization
headers = {"Accept": "application/json"}
session = aiohttp.ClientSession(cookie_jar=jar)
resp = await session.get("https://iot.quasar.yandex.ru/...", headers=headers)
```

### ❌ Неправильно

```python
# ЭТО НЕ РАБОТАЕТ!
token = await get_oauth_token()
headers = {"Authorization": f"Bearer {token}"}  # ❌
resp = await session.get("https://iot.quasar.yandex.ru/...", headers=headers)
# → HTTP 401 Unauthorized
```

## Диагностика

```bash
# Проверить cookies
curl http://localhost:8000/oauth/yandex/cookies

# Если пусто → установить
python3 dev-scripts/set_yandex_cookies.py

# Посмотреть логи
# Должно быть: [INFO] Quasar WS connected
# Не должно быть: [ERROR] HTTP 401
```

## Правило

```
╔════════════════════════════════════════╗
║  Quasar = Cookies ONLY, NO OAuth!     ║
╚════════════════════════════════════════╝
```

Подробности: `QUASAR_ARCHITECTURE_RULE.md`
