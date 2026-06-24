from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CloudFile:
    name: str
    path: str
    size: int
    is_dir: bool
    modified: str = ""
    mime_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class CloudProvider(ABC):
    """Базовый интерфейс облачного провайдера."""

    name: str  # идентификатор: yandex_disk, google_drive, dropbox

    @abstractmethod
    async def check(self) -> bool:
        """Проверить подключение (валидность токена/сессии)."""

    @abstractmethod
    async def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        """Загрузить файл. Возвращает {"success": bool, "path": str, "size": int}."""

    @abstractmethod
    async def download(self, remote_path: str) -> bytes:
        """Скачать файл. Поднимает исключение если файл не найден."""

    @abstractmethod
    async def list_files(self, remote_path: str = "") -> list[CloudFile]:
        """Список файлов/папок по пути."""

    @abstractmethod
    async def delete(self, remote_path: str) -> bool:
        """Удалить файл/папку. Возвращает True при успехе."""

    async def close(self) -> None:
        """Закрыть сессию (опционально)."""
