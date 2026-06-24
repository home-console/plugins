# АРХИТЕКТУРНОЕ ПРАВИЛО: Quasar API

## ⚠️ КРИТИЧЕСКИ ВАЖНО ДЛЯ ВСЕХ РАЗРАБОТЧИКОВ

Это НЕ рекомендация, это **жёсткое архитектурное правило**, которое НЕЛЬЗЯ нарушать.

---

## Правило #1: Quasar НЕ использует OAuth

```python
# ❌ НЕПРАВИЛЬНО — НЕ ДЕЛАЙТЕ ТАК НИКОГДА!
headers = {
    "Authorization": f"Bearer {access_token}",  # ОШИБКА!
}
requests.get("https://iot.quasar.yandex.ru/...", headers=headers)
```

```python
# ✅ ПРАВИЛЬНО — ТОЛЬКО cookies
jar = aiohttp.CookieJar()
# ... загрузка cookies из storage ...
session = aiohttp.ClientSession(cookie_jar=jar)
session.get("https://iot.quasar.yandex.ru/...")
```

---

## Почему так?

### Два разных API Яндекса:

| API | URL | Авторизация | Назначение |
|-----|-----|-------------|-----------|
| **Публичный OAuth API** | `api.iot.yandex.net` | `Authorization: Bearer <token>` | Официальный API для сторонних приложений |
| **Quasar API** | `iot.quasar.yandex.ru` | **Cookies сессии** | Внутренний API для мобильного приложения |

### Quasar API — это reverse-engineered внутренний API

Quasar используется в:
- Мобильном приложении "Яндекс" (iOS/Android)
- Колонках Яндекс.Станция
- Внутренних сервисах Яндекса

**Он НЕ документирован публично** и работает как браузерная сессия:
- Авторизация через cookies (`Session_id`, `yandexuid`, `sessionid2`)
- Нет OAuth flow
- Нет публичных токенов
- Те же cookies, что используются на yandex.ru

### Аналогия

```
Quasar API работает как:
1. Вы заходите на yandex.ru в браузере
2. Логинитесь (получаете cookies)
3. Дальше все запросы используют эти cookies

OAuth API работает как:
1. Приложение запрашивает разрешения
2. Пользователь подтверждает
3. Приложение получает токен
4. Токен используется в Authorization header
```

---

## Обязательные cookies

```json
{
  "Session_id": "3:1234567890.5.0.xxx:yyy:zzz",  // ОБЯЗАТЕЛЬНО
  "yandexuid": "1234567890123456789",           // ОБЯЗАТЕЛЬНО
  "sessionid2": "3:1234567890.5.0.xxx:yyy:zzz", // Желательно
  "i": "...",                                   // Опционально
  "L": "..."                                    // Опционально
}
```

### Как получить cookies?

1. Откройте https://yandex.ru в браузере
2. Войдите в свой аккаунт
3. DevTools (F12) → Application → Cookies → https://yandex.ru
4. Скопируйте значения
5. Установите через:
   ```bash
   python3 dev-scripts/set_yandex_cookies.py
   ```

---

## Что НЕЛЬЗЯ делать

### ❌ Использовать OAuth token для Quasar

```python
# ЭТО ВСЕГДА ВЕРНЁТ 401 UNAUTHORIZED
access_token = await get_access_token()
headers = {"Authorization": f"Bearer {access_token}"}
resp = await session.get("https://iot.quasar.yandex.ru/...", headers=headers)
# resp.status == 401
```

### ❌ Пытаться "починить" 401 через OAuth refresh

```python
# НЕ ДЕЛАЙТЕ ТАК!
if resp.status == 401:
    # Обновить OAuth token НЕ ПОМОЖЕТ!
    access_token = await refresh_oauth_token()  # ❌ Бесполезно
```

### ❌ Смешивать OAuth и cookies

```python
# НЕ ДЕЛАЙТЕ ТАК!
headers = {
    "Authorization": f"Bearer {token}",  # ❌ Не нужен
}
session = aiohttp.ClientSession(cookie_jar=jar)  # ✅ Это работает
```

---

## Что НУЖНО делать

### ✅ Проверять наличие cookies перед запросом

```python
cookies = await load_cookies()
if not cookies:
    raise RuntimeError("Cookies required for Quasar API")

required = ["Session_id", "yandexuid"]
missing = [k for k in required if k not in cookies]
if missing:
    raise RuntimeError(f"Missing cookies: {missing}")
```

### ✅ Использовать CookieJar

```python
jar = aiohttp.CookieJar()
for name, value in cookies.items():
    jar.update_cookies({name: value}, response_url="https://iot.quasar.yandex.ru")

session = aiohttp.ClientSession(cookie_jar=jar)
```

### ✅ При 401/403 проверять cookies, а не OAuth

```python
if resp.status in (401, 403):
    # Проблема в cookies, НЕ в OAuth!
    cookies = await load_cookies()
    if not cookies:
        raise RuntimeError("Cookies expired or missing. Get fresh cookies from browser.")
```

---

## Защита от ошибок

В коде добавлены `assert` для предотвращения случайных ошибок:

```python
async def _fetch_devices_and_url(self, cookies: Dict[str, str]):
    headers = {"Accept": "application/json"}
    # Защита от случайного добавления Authorization
    assert "Authorization" not in headers, "NEVER use OAuth with Quasar API!"
    # ...
```

Если вы увидите `AssertionError: NEVER use OAuth with Quasar API!` — это значит кто-то попытался добавить OAuth заголовок.

---

## Диагностика проблем

### HTTP 401 Unauthorized

**Причина:** Cookies истекли или неправильные

**Решение:**
1. Получить свежие cookies из браузера
2. Установить через `set_yandex_cookies.py`
3. Перезапустить runtime

### Cookies not found

**Причина:** Cookies не установлены в storage

**Решение:**
```bash
python3 dev-scripts/set_yandex_cookies.py
```

### WebSocket не подключается

**Причина:** Чаще всего — проблема с cookies

**Проверка:**
```bash
curl -X GET http://localhost:8000/oauth/yandex/cookies
```

Если пустой ответ — cookies не установлены.

---

## Валидные примеры кода

### ✅ Правильный запрос к Quasar

```python
async def fetch_quasar_devices():
    # Загружаем cookies
    cookies = await self.call_service("oauth_yandex.get_cookies")
    if not cookies:
        raise RuntimeError("Cookies required")
    
    # Создаём CookieJar
    jar = aiohttp.CookieJar()
    parsed = urlparse("https://iot.quasar.yandex.ru")
    for name, value in cookies.items():
        jar.update_cookies({name: value}, response_url=parsed.geturl())
    
    # Запрос БЕЗ Authorization header
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 ..."
    }
    
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.get(
            "https://iot.quasar.yandex.ru/m/v3/user/devices",
            headers=headers
        ) as resp:
            return await resp.json()
```

### ✅ Правильное WebSocket соединение

```python
async def connect_quasar_ws():
    cookies = await load_cookies()
    jar = create_cookie_jar(cookies)
    
    headers = {
        "Origin": "https://iot.quasar.yandex.ru",
        "User-Agent": "Mozilla/5.0 ..."
    }
    # БЕЗ Authorization!
    
    session = aiohttp.ClientSession(cookie_jar=jar)
    ws = await session.ws_connect(updates_url, headers=headers)
    # ...
```

---

## Последствия нарушения правила

Если добавить `Authorization: Bearer` заголовок к Quasar запросам:

1. ✅ **Запрос уйдёт** (HTTP клиент отправит)
2. ❌ **Сервер вернёт 401** (токен не валиден для Quasar)
3. ❌ **OAuth refresh НЕ поможет** (новый токен тоже не сработает)
4. ❌ **Система будет в бесконечном retry** (backoff, но без результата)
5. ❌ **Realtime обновления НЕ работают** (WebSocket не подключится)

**Единственное решение:** убрать OAuth, использовать cookies.

---

## Референсы

- [YandexStation by AlexxIT](https://github.com/AlexxIT/YandexStation) — аналогичная реализация для Home Assistant, использует cookies
- [Quasar API implementation](https://github.com/AlexxIT/YandexStation/blob/master/custom_components/yandex_station/core/yandex_quasar.py) — код для изучения

---

## Контрольный чеклист для code review

При добавлении/изменении кода Quasar проверьте:

- [ ] НЕТ строки `Authorization` в headers для `iot.quasar.yandex.ru`
- [ ] НЕТ строки `Bearer` в headers для `iot.quasar.yandex.ru`
- [ ] Используется `CookieJar` с загруженными cookies
- [ ] Есть проверка наличия cookies перед запросом
- [ ] При 401 НЕ вызывается OAuth refresh
- [ ] При 401 проверяются/обновляются cookies
- [ ] В комментариях явно указано "NO OAuth"
- [ ] Есть логирование используемых cookie names

---

## Вопросы и ответы

**Q: Почему бы не использовать OAuth? Это же проще.**

A: OAuth **физически не работает** с Quasar API. Сервер вернёт 401. Это не наш выбор, это архитектура Яндекса.

**Q: Может, Яндекс добавит OAuth поддержку в Quasar?**

A: Маловероятно. Quasar — внутренний API для собственных приложений. Для сторонних разработчиков есть публичный OAuth API.

**Q: Можно ли использовать оба API одновременно?**

A: Да! Для разных целей:
- OAuth API → управление устройствами (команды)
- Quasar API → realtime обновления (WebSocket)

**Q: Как часто истекают cookies?**

A: Обычно несколько месяцев. Истекают при выходе из аккаунта или смене пароля.

**Q: Безопасно ли хранить cookies?**

A: Cookies = полный доступ к аккаунту. Храните в зашифрованном storage, не логируйте значения, не коммитьте в git.

---

## Финальное напоминание

```
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   ⚠️  Quasar API НЕ ИСПОЛЬЗУЕТ OAuth Bearer token  ⚠️   ║
║                                                          ║
║   Только cookies!                                        ║
║   Это архитектурное правило, не рекомендация.           ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
```

Если сомневаетесь — спросите у senior разработчика перед изменением кода.
