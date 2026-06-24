from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import CloudFile, CloudProvider

logger = logging.getLogger(__name__)

_BASE = "https://cloud-api.yandex.net/v1/disk"


class YandexDiskProvider(CloudProvider):
    name = "yandex_disk"

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"Authorization": f"OAuth {token}"},
        )

    async def check(self) -> bool:
        try:
            r = await self._client.get(f"{_BASE}/")
            return r.status_code == 200
        except Exception as e:
            logger.warning("yandex_disk.check failed: %s", e)
            return False

    async def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        try:
            r = await self._client.get(
                f"{_BASE}/resources/upload",
                params={"path": remote_path, "overwrite": "true"},
            )
            r.raise_for_status()
            upload_url = r.json()["href"]

            put = await self._client.put(upload_url, content=data)
            if put.status_code in (201, 202):
                return {"success": True, "path": remote_path, "size": len(data)}
            return {"success": False, "error": f"HTTP {put.status_code}: {put.text}"}
        except Exception as e:
            logger.error("yandex_disk.upload error: %s", e)
            return {"success": False, "error": str(e)}

    async def download(self, remote_path: str) -> bytes:
        r = await self._client.get(
            f"{_BASE}/resources/download",
            params={"path": remote_path},
        )
        r.raise_for_status()
        dl_url = r.json()["href"]

        data = await self._client.get(dl_url)
        data.raise_for_status()
        return data.content

    async def list_files(self, remote_path: str = "") -> list[CloudFile]:
        try:
            r = await self._client.get(
                f"{_BASE}/resources",
                params={"path": remote_path or "/"},
            )
            r.raise_for_status()
            items = r.json().get("_embedded", {}).get("items", [])
            return [
                CloudFile(
                    name=it["name"],
                    path=it["path"].removeprefix("disk:"),
                    size=it.get("size", 0),
                    is_dir=it["type"] == "dir",
                    modified=it.get("modified", ""),
                    mime_type=it.get("mime_type", ""),
                )
                for it in items
            ]
        except Exception as e:
            logger.error("yandex_disk.list_files error: %s", e)
            return []

    async def delete(self, remote_path: str) -> bool:
        try:
            r = await self._client.delete(
                f"{_BASE}/resources",
                params={"path": remote_path, "permanently": "true"},
            )
            return r.status_code == 204
        except Exception as e:
            logger.error("yandex_disk.delete error: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
