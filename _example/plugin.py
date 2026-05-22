"""Минимальный шаблон плагина для Home Console.

Скопируй папку `_example/` в `my_plugin_name/`, поменяй `name` в `plugin.json`,
переименуй класс ниже и реализуй свою логику.
"""

from __future__ import annotations

from typing import Any


class ExamplePlugin:
    """Шаблон плагина.

    Home Console инстанциирует класс при загрузке. Минимум должен иметь
    `start()` и `stop()`. Всё остальное — по необходимости.
    """

    def __init__(self, ctx: Any | None = None) -> None:
        self.ctx = ctx

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
