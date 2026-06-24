# Yandex Smart Home Plugin

Интеграция с Яндекс Умным домом через официальный OAuth API и внутренний Quasar API.

## Возможности

- ✅ Синхронизация устройств из Яндекс аккаунта
- ✅ Управление устройствами (включение/выключение)
- ✅ **Realtime обновления через Quasar WebSocket**
- ✅ Автоматическое обновление состояний устройств

## Архитектура

Плагин использует **ДВА разных API** Яндекса:

### 1. OAuth API (`api.iot.yandex.net`)
- **Авторизация:** OAuth 2.0 Bearer token
- **Использование:** Команды управления, initial sync
- **Статус:** Официальный публичный API

### 2. Quasar API (`iot.quasar.yandex.ru`)
- **Авторизация:** ⚠️ Cookies сессии (НЕ OAuth!)
- **Использование:** Realtime WebSocket обновления
- **Статус:** Внутренний reverse-engineered API

## ⚠️ ВАЖНО: Quasar API

**Quasar API НЕ использует OAuth Bearer token!**

Это внутренний API Яндекса, который работает через cookies сессии.
Попытка использовать OAuth приведёт к HTTP 401 Unauthorized.

### Быстрый старт Quasar

```bash
# 1. Получить cookies из браузера (yandex.ru)
# 2. Установить cookies
python3 dev-scripts/set_yandex_cookies.py

# 3. Проверить
curl http://localhost:8000/oauth/yandex/cookies

# 4. Перезапустить runtime
python3 main.py
```

## Документация

- **[QUASAR_CHEATSHEET.md](QUASAR_CHEATSHEET.md)** — Быстрая справка
- **[QUASAR_WEBSOCKET.md](QUASAR_WEBSOCKET.md)** — Руководство пользователя
- **[QUASAR_ARCHITECTURE_RULE.md](QUASAR_ARCHITECTURE_RULE.md)** — Архитектурные правила
- **[IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)** — Технические детали

## Установка

1. **Настроить OAuth (для команд управления):**
   ```bash
   curl -X POST http://localhost:8000/oauth/yandex/configure \
     -H "Content-Type: application/json" \
     -d '{"client_id":"...", "client_secret":"...", "redirect_uri":"..."}'
   ```

2. **Авторизоваться через OAuth:**
   ```bash
   # Получить URL авторизации
   curl http://localhost:8000/oauth/yandex/authorize-url
   
   # Перейти по URL в браузере, получить code
   
   # Обменять code на токены
   curl -X POST http://localhost:8000/oauth/yandex/exchange-code \
     -H "Content-Type: application/json" \
     -d '{"code":"..."}'
   ```

3. **Установить cookies (для realtime обновлений):**
   ```bash
   python3 dev-scripts/set_yandex_cookies.py
   ```

4. **Включить использование реального API:**
   ```python
   await self.storage_set("yandex", "use_real_api", {"enabled": True})
   ```

5. **Синхронизировать устройства:**
   ```bash
   curl -X POST http://localhost:8000/admin/v1/yandex/sync
   ```

## Сервисы

### OAuth API
- `yandex.sync_devices` — Синхронизация устройств
- `yandex.check_devices_online` — Проверка онлайн статуса

### Quasar WebSocket
- `yandex.subscribe_device_updates(device_id, callback)` — Подписка на обновления

### Cookies (oauth_yandex)
- `oauth_yandex.set_cookies(cookies)` — Установить cookies
- `oauth_yandex.get_cookies()` — Получить cookies

## События

- `external.device_discovered` — Обнаружено новое устройство
- `external.device_state_reported` — Обновление состояния устройства
- `internal.device_command_requested` — Запрос на выполнение команды

## Troubleshooting

### HTTP 401 Unauthorized от Quasar

**Причина:** Используется OAuth вместо cookies, или cookies истекли

**Решение:**
```bash
python3 dev-scripts/set_yandex_cookies.py
```

### WebSocket не подключается

**Проверка:**
```bash
# Проверить наличие cookies
curl http://localhost:8000/oauth/yandex/cookies

# Проверить feature flag
# должен быть {"enabled": true}
```

### Устройства не обновляются в realtime

**Диагностика:**
1. Проверить логи: должно быть `[INFO] Quasar WS connected`
2. Проверить события: `external.device_state_reported`
3. Проверить cookies: могли истечь

## Разработка

### Правила кода

⚠️ **КРИТИЧНО:** При работе с Quasar API:
- ❌ НИКОГДА не добавляйте `Authorization: Bearer` заголовок
- ✅ Используйте ТОЛЬКО cookies через `CookieJar`
- ✅ Проверяйте наличие cookies перед запросом
- ✅ При 401 проверяйте cookies, НЕ OAuth

См. [QUASAR_ARCHITECTURE_RULE.md](QUASAR_ARCHITECTURE_RULE.md)

### Структура файлов

```
yandex_smart_home/
├── plugin.py                     # Главный плагин
├── clients/
│   ├── api_client.py             # Quasar HTTP API клиент (cookies!)
│   └── yandex_quasar_ws.py       # Quasar WebSocket (cookies!)
├── sync/
│   ├── device_sync.py            # Синхронизация устройств
│   └── device_status.py          # Проверка статуса
├── command_handler.py            # Обработка команд
├── transformers/
│   └── device_transformer.py     # Трансформация данных
└── docs/
    ├── QUASAR_CHEATSHEET.md     # Быстрая справка
    ├── QUASAR_WEBSOCKET.md      # Руководство
    ├── QUASAR_ARCHITECTURE_RULE.md  # Архитектура
    └── IMPLEMENTATION_SUMMARY.md    # Детали реализации
```

## Безопасность

⚠️ **Session cookies дают полный доступ к аккаунту Яндекса!**

- Храните cookies в безопасности
- Не логируйте значения cookies
- Не коммитьте в git
- При компрометации — сменить пароль

## Лицензия

Home Console Project

## Автор

Home Console Team

---

**Документация обновлена:** 24 января 2026
