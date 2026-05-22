# _example

Шаблон плагина. **Не публикуется** в маркетплейс — папка пропускается CI,
поскольку начинается с `_`.

## Как использовать

```bash
cp -r _example my_plugin_name
cd my_plugin_name
```

Затем:

1. Открой `plugin.json`, замени `name` на `my_plugin_name` (snake_case, должно
   совпадать с именем папки).
2. Заполни `description`, `author`, при необходимости — `role`, `namespace`,
   `provides_services`, `provides_events` и т.д.
3. Переименуй класс `ExamplePlugin` в `plugin.py` (если хочешь) и обнови
   `class_path` в `plugin.json`.
4. Реализуй свою логику в `plugin.py`.
5. Если плагин зависит от других — перечисли их в `dependencies`.

## Минимальный API класса

```python
class MyPlugin:
    def __init__(self, ctx): ...   # ctx — runtime-контекст (опц.)
    def start(self): ...           # вызывается при загрузке
    def stop(self): ...            # вызывается при выгрузке
```

Остальное — по архитектуре конкретного плагина: capability provider, integration,
event handler и т.д.
