from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .base import CloudFile, CloudProvider

logger = logging.getLogger(__name__)

_API = "https://api.dropboxapi.com/2"
_CONTENT = "https://content.dropboxapi.com/2"


class DropboxProvider(CloudProvider):
    name = "dropbox"

    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._client = httpx.AsyncClient(
            timeout=60.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def check(self) -> bool:
        try:
            r = await self._client.post(f"{_API}/users/get_current_account")
            return r.status_code == 200
        except Exception as e:
            logger.warning("dropbox.check failed: %s", e)
            return False

    async def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        try:
            path = remote_path if remote_path.startswith("/") else f"/{remote_path}"
            args = json.dumps({"path": path, "mode": "overwrite", "autorename": False})
            r = await self._client.post(
                f"{_CONTENT}/files/upload",
                content=data,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Dropbox-API-Arg": args,
                    "Content-Type": "application/octet-stream",
                },
            )
            r.raise_for_status()
            meta = r.json()
            return {"success": True, "path": meta.get("path_display", remote_path), "size": len(data)}
        except Exception as e:
            logger.error("dropbox.upload error: %s", e)
            return {"success": False, "error": str(e)}

    async def download(self, remote_path: str) -> bytes:
        path = remote_path if remote_path.startswith("/") else f"/{remote_path}"
        args = json.dumps({"path": path})
        r = await self._client.post(
            f"{_CONTENT}/files/download",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Dropbox-API-Arg": args,
            },
        )
        r.raise_for_status()
        return r.content

    async def list_files(self, remote_path: str = "") -> list[CloudFile]:
        try:
            path = remote_path if remote_path.startswith("/") else f"/{remote_path}"
            if path == "/":
                path = ""
            r = await self._client.post(
                f"{_API}/files/list_folder",
                json={"path": path, "recursive": False, "include_media_info": False},
            )
            r.raise_for_status()
            entries = r.json().get("entries", [])
            result = []
            while True:
                for e in entries:
                    result.append(CloudFile(
                        name=e["name"],
                        path=e.get("path_display", ""),
                        size=e.get("size", 0),
                        is_dir=e[".tag"] == "folder",
                        modified=e.get("server_modified", ""),
                    ))
                body = r.json()
                if not body.get("has_more"):
                    break
                r = await self._client.post(
                    f"{_API}/files/list_folder/continue",
                    json={"cursor": body["cursor"]},
                )
                r.raise_for_status()
                entries = r.json().get("entries", [])
            return result
        except Exception as e:
            logger.error("dropbox.list_files error: %s", e)
            return []

    async def delete(self, remote_path: str) -> bool:
        try:
            path = remote_path if remote_path.startswith("/") else f"/{remote_path}"
            r = await self._client.post(f"{_API}/files/delete_v2", json={"path": path})
            return r.status_code == 200
        except Exception as e:
            logger.error("dropbox.delete error: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
