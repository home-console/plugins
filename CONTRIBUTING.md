# Contributing

Спасибо что хочешь добавить плагин в `home-console/plugins`. Прочти этот гайд
перед первым PR — это сэкономит время и тебе, и ревьюерам.

## Quick start

```bash
git clone https://github.com/home-console/plugins
cd plugins
cp -r _example my_plugin_name
cd my_plugin_name
$EDITOR plugin.json  # отредактируй name/version/description/author
```

Затем `git add`, `git commit`, `git push`, открой PR.

## Требования к плагину

### Структура каталога

```
my_plugin_name/
├── plugin.json          ← обязательно, в корне папки плагина
├── __init__.py
├── plugin.py            ← главный класс, путь указывается в plugin.json.class_path
├── README.md            ← опционально, описание для каталога
└── ... (любой Python-код)
```

### `plugin.json`

Обязательные поля:

| поле | тип | пример |
|---|---|---|
| `name` | string (snake_case) | `"oauth_yandex"` |
| `version` | semver | `"0.1.0"` |
| `description` | string | `"OAuth helper для Яндекса"` |
| `class_path` | string (Python dotted) | `"plugins.oauth_yandex.plugin.OAuthYandexPlugin"` или короткий `"plugin.MyPlugin"` |

`name` **должен совпадать с именем папки**. Если не совпадает, CI зарежет PR.

`class_path` поддерживает два формата:
- абсолютный: `plugins.<name>.<module>.<ClassName>` (как у `oauth_yandex`)
- короткий: `<module>.<ClassName>` (как у `remote_plugin_proxy`, считается
  относительно корня папки плагина)

Опционально: `role`, `dependencies`, `min_runtime`, `provides_services`,
`provides_events`, `storage_namespaces`, `namespace`, `capability`,
`is_integration`, `integration_name`, `integration_flags`, `type`, `tags`,
`execution_mode`, `dynamic_service_registration`, `user_facing`.

## CI: что проверяется на PR

1. `plugin.json` парсится как валидный JSON.
2. Обязательные поля присутствуют и непустые.
3. `name` совпадает с именем папки.
4. `version` — валидный semver.
5. `class_path` указывает на существующий файл (`plugins.<name>.<module>.Class`).
6. Все файлы из `manifest` (если есть) присутствуют.

## Что происходит после мержа

GitHub Action `publish.yml` определяет какие плагины изменились в коммите,
и для каждого зовёт:

```
POST https://marketplace.homeconsole.su/api/plugins/<name>/releases/from-git
{
  "ref": "<merge_commit_sha>",
  "channel": "stable",
  "subpath": "<name>",
  "force_replace": true
}
```

Marketplace сам:
- скачивает zipball коммита,
- выдёргивает подпапку плагина,
- считает sha256,
- подписывает релиз своим Ed25519 ключом,
- публикует в каталог.

Никаких ручных подписей и zip-загрузок.

## Версионирование

- Меняешь код, не меняя `version` → следующий публикуется как **replace** той же версии
  (для dev-цикла, удобно пока плагин в активной разработке).
- Меняешь `version` → публикуется как новый релиз.
- Канал по умолчанию — `stable`. Для бета-веток меняй на `beta` в `plugin.json`.

## Code review

- Один PR = один плагин (если только это не общий рефакторинг по всем).
- Реквизиты PR-шаблона должны быть заполнены.
- Code owners (`CODEOWNERS`) ревьюят свои подпапки. Без аппрува merge невозможен.

## Лицензия

Контрибутя в этот репозиторий, ты соглашаешься, что твой код будет распространяться
под MIT (см. [LICENSE](./LICENSE)).
