# Quasar WebSocket — Realtime обновления устройств

## Проблема с авторизацией

**ВАЖНО:** Quasar API (`iot.quasar.yandex.ru`) — это **reverse-engineered внутренний API** Яндекса, который требует **cookies сессии**, а **НЕ OAuth Bearer токен**.

### Почему OAuth не работает?

API Яндекс Умного дома имеет два разных endpoint'а:

1. **Публичный OAuth API** (`api.iot.yandex.net`) — официальный API, использует `Authorization: Bearer <token>`
2. **Quasar API** (`iot.quasar.yandex.ru`) — внутренний API приложения/колонок, использует **cookies сессии**

Quasar API используется для:
- WebSocket realtime-обновлений (`updates_url`)
- Расширенной информации об устройствах
- Функций недоступных в публичном API

### Как получить cookies?

1. Откройте браузер (Chrome/Firefox)
2. Перейдите на https://yandex.ru
3. Войдите в свой аккаунт Яндекса
4. Откройте DevTools (F12)
5. Перейдите: Application → Cookies → https://yandex.ru
6. Скопируйте значения:
   - `Session_id` (обязательно)
   - `yandexuid` (обязательно)
   - `sessionid2` (желательно)
   - `i` (опционально)
   - `L` (опционально)

### Установка cookies

#### Способ 1: Через Python скрипт

```bash
cd /Users/misha/HomeConsole/core-runtime-service
python3 dev-scripts/set_yandex_cookies.py
```

Следуйте инструкциям на экране.

#### Способ 2: Через HTTP API

```bash
curl -X POST http://localhost:8000/oauth/yandex/cookies \
  -H "Content-Type: application/json" \
  -d '{
    "Session_id": "ваш_session_id_тут",
    "yandexuid": "ваш_yandexuid_тут",
    "sessionid2": "ваш_sessionid2_тут"
  }'
```

#### Способ 3: Напрямую через storage (для продвинутых)

```python
await self.storage_set("yandex", "cookies", {
    "Session_id": "...",
    "yandexuid": "...",
    "sessionid2": "..."
})
```

### Проверка работы

После установки cookies перезапустите `core-runtime`:

```bash
python3 main.py
```

В логах должно быть:

```
[INFO] [yandex_smart_home] Quasar WS connected
```

Если видите:
```
[ERROR] [yandex_smart_home] Quasar WS: cookies required but not found
```

Значит cookies не установлены или устарели.

### Как долго действуют cookies?

Cookies Яндекса обычно действуют несколько месяцев, но могут истечь при:
- Выходе из аккаунта
- Смене пароля
- Долгом неиспользовании

При истечении cookies увидите `HTTP 401: Unauthorized` в логах. Нужно будет получить свежие cookies заново.

### Архитектура

```
┌─────────────────────────────────────────────┐
│ yandex_smart_home plugin                    │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │ REST API (api.iot.yandex.net)        │  │
│  │ • OAuth Bearer token                 │  │
│  │ • Initial device sync                │  │
│  │ • Device commands                    │  │
│  └──────────────────────────────────────┘  │
│                                             │
│  ┌──────────────────────────────────────┐  │
│  │ Quasar WS (iot.quasar.yandex.ru)     │  │
│  │ • Session cookies                    │  │
│  │ • Realtime updates                   │  │
│  │ • No OAuth token                     │  │
│  └──────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

### Сервисы

**oauth_yandex плагин:**
- `oauth_yandex.set_cookies(cookies: dict)` — сохранить cookies
- `oauth_yandex.get_cookies()` — получить cookies

**yandex_smart_home плагин:**
- `yandex.subscribe_device_updates(device_id, callback)` — подписка на обновления устройства

### Безопасность

⚠️ **ВАЖНО:** Session cookies дают полный доступ к вашему аккаунту Яндекса!

- Храните их в безопасности
- Не делитесь ими
- Используйте только на доверенных устройствах
- При компрометации немедленно смените пароль Яндекса

### Альтернативы

Если не хотите использовать cookies:
1. Используйте только REST API (без realtime-обновлений)
2. Включите polling (менее эффективно)
3. Дождитесь официального WebSocket API от Яндекса (если появится)

### Troubleshooting

**Ошибка: `HTTP 401: Unauthorized`**
- Cookies истекли или неправильные
- Получите свежие cookies из браузера

**Ошибка: `cookies required but not found`**
- Cookies не установлены
- Используйте `set_yandex_cookies.py` или API

**WebSocket не подключается**
- Проверьте что `yandex.use_real_api` включён в storage
- Проверьте сетевое подключение
- Проверьте логи на детали ошибки

**Устройства не обновляются**
- WS подключён? Проверьте лог "Quasar WS connected"
- События `external.device_state_reported` публикуются?
- DevicesModule обрабатывает события?

### Дальнейшая разработка

- [ ] Автоматическое обновление cookies через headless browser
- [ ] Watchdog для определения "мёртвого" WS канала
- [ ] Graceful fallback на polling при проблемах с WS
- [ ] Метрики: количество обновлений, lag, reconnect rate
- [ ] Поддержка online/offline из WS-обновлений

### Ссылки

- [YandexStation by AlexxIT](https://github.com/AlexxIT/YandexStation) — аналогичная реализация для Home Assistant
- [Quasar API analysis](https://github.com/AlexxIT/YandexStation/blob/master/custom_components/yandex_station/core/yandex_quasar.py)
