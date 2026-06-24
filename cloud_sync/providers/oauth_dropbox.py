"""
DropboxProvider через oauth_dropbox плагин.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

from .base import CloudFile, CloudProvider

if TYPE_CHECKING:
    from sdk.plugin_ext import BasePlugin

logger = logging.getLogger(__name__)

_API     = "https://api.dropboxapi.com/2"
_CONTENT = "https://content.dropboxapi.com/2"


class DropboxOAuthProvider(CloudProvider):
    """Dropbox через oauth_dropbox плагин."""

    name = "dropbox"

    def __init__(self, plugin: "BasePlugin") -> None:
        self._plugin = plugin
        self._client = httpx.AsyncClient(timeout=60.0)

    async def _token(self) -> str:
        result = await self._plugin.call_service("oauth_dropbox.get_access_token")
        token = result if isinstance(result, str) else (result or {}).get("access_token", "")
        if not token:
            raise RuntimeError("oauth_dropbox не вернул access_token")
        return token

    async def check(self) -> bool:
        try:
            token = await self._token()
            r = await self._client.post(
                f"{_API}/users/get_current_account",
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code == 200
        except Exception as e:
            logger.warning("dropbox(oauth): check failed: %s", e)
            return False

    async def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        try:
            token = await self._token()
            path  = remote_path if remote_path.startswith("/") else f"/{remote_path}"
            args  = json.dumps({"path": path, "mode": "overwrite", "autorename": False})
            r     = await self._client.post(
                f"{_CONTENT}/files/upload",
                content=data,
                headers={
                    "Authorization":    f"Bearer {token}",
                    "Dropbox-API-Arg":  args,
                    "Content-Type":     "application/octet-stream",
                },
            )
            r.raise_for_status()
            meta = r.json()
            return {"success": True, "path": meta.get("path_display", remote_path), "size": len(data)}
        except Exception as e:
            logger.error("dropbox(oauth).upload error: %s", e)
            return {"success": False, "error": str(e)}

    async def download(self, remote_path: str) -> bytes:
        token = await self._token()
        path  = remote_path if remote_path.startswith("/") else f"/{remote_path}"
        r     = await self._client.post(
            f"{_CONTENT}/files/download",
            headers={"Authorization": f"Bearer {token}", "Dropbox-API-Arg": json.dumps({"path": path})},
        )
        r.raise_for_status()
        return r.content

    async def list_files(self, remote_path: str = "") -> list[CloudFile]:
        try:
            token = await self._token()
            path  = remote_path if remote_path.startswith("/") else f"/{remote_path}"
            if path == "/":
                path = ""
            r      = await self._client.post(
                f"{_API}/files/list_folder",
                json={"path": path, "recursive": False},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            return [
                CloudFile(name=e["name"], path=e.get("path_display", ""),
                          size=e.get("size", 0), is_dir=e[".tag"] == "folder",
                          modified=e.get("server_modified", ""))
                for e in r.json().get("entries", [])
            ]
        except Exception as e:
            logger.error("dropbox(oauth).list_files error: %s", e)
            return []

    async def delete(self, remote_path: str) -> bool:
        try:
            token = await self._token()
            path  = remote_path if remote_path.startswith("/") else f"/{remote_path}"
            r     = await self._client.post(
                f"{_API}/files/delete_v2",
                json={"path": path},
                headers={"Authorization": f"Bearer {token}"},
            )
            return r.status_code == 200
        except Exception as e:
            logger.error("dropbox(oauth).delete error: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
