# home-console/plugins

Официальная коллекция плагинов для [Home Console](https://homeconsole.su).

Каждая папка верхнего уровня (кроме служебных `_example/`, `scripts/`, `.github/`) —
это отдельный плагин с собственным `plugin.json`. Структура:

```
plugins/
├── oauth_yandex/
│   ├── plugin.json
│   ├── plugin.py
│   └── ...
├── network_scanner/
│   ├── plugin.json
│   └── ...
└── _example/         ← шаблон, по образцу которого делается новый плагин
```

## Как опубликовать новый плагин

1. Сделай форк репозитория.
2. Скопируй `_example/` в `my_plugin_name/` (snake_case, должно совпадать с
   `plugin.json.name`).
3. Заполни `plugin.json` и код.
4. Открой Pull Request в этот репозиторий.
5. CI проверит схему `plugin.json`. После ревью и мержа в `master` —
   GitHub Action автоматически опубликует новую версию в реестре
   [marketplace.homeconsole.su](https://marketplace.homeconsole.su).

Подробнее: [CONTRIBUTING.md](./CONTRIBUTING.md).

## Структура `plugin.json` (минимум)

```json
{
  "name": "my_plugin_name",
  "version": "0.1.0",
  "description": "Что делает плагин",
  "author": "Your Name",
  "class_path": "plugins.my_plugin_name.plugin.MyPlugin",
  "role": "capability_provider",
  "dependencies": []
}
```

Полная схема — в [`_example/plugin.json`](./_example/plugin.json) и в
[`marketplace-api`](https://github.com/home-console/home-console) спеках.

## Архитектурный принцип

```
git push → home-console/plugins → CI → POST /releases/from-git → marketplace.homeconsole.su
                                                                          │
                                                                          ▼
                                                            core-runtime-service
                                                            (install-from-registry)
```

Git — источник правды для исходников. Marketplace — дистрибьютор подписанных
артефактов. Core никогда не ходит в Git напрямую.

## Лицензия

MIT — см. [LICENSE](./LICENSE).
