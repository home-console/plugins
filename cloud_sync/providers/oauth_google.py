"""
GoogleDriveProvider через oauth_google плагин.
Токен берётся из сервиса oauth_google.get_access_token — авто-refresh,
UI авторизации, хранение в storage — всё в oauth_google, не здесь.
"""
from __future__ import annotations

import logging
import mimetypes
from typing import TYPE_CHECKING, Any

import httpx

from .base import CloudFile, CloudProvider

if TYPE_CHECKING:
    from sdk.plugin_ext import BasePlugin

logger = logging.getLogger(__name__)

_API    = "https://www.googleapis.com/drive/v3"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"


class GoogleDriveOAuthProvider(CloudProvider):
    """Google Drive через oauth_google плагин (рекомендуемый способ)."""

    name = "google_drive"

    def __init__(self, plugin: "BasePlugin") -> None:
        self._plugin = plugin
        self._client = httpx.AsyncClient(timeout=60.0)

    async def _token(self) -> str:
        result = await self._plugin.call_service("oauth_google.get_access_token")
        token = result if isinstance(result, str) else (result or {}).get("access_token", "")
        if not token:
            raise RuntimeError("oauth_google не вернул access_token — возможно нужна авторизация")
        return token

    def _auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def check(self) -> bool:
        try:
            token = await self._token()
            r = await self._client.get(f"{_API}/about?fields=user", headers=self._auth(token))
            return r.status_code == 200
        except Exception as e:
            logger.warning("google_drive(oauth): check failed: %s", e)
            return False

    async def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        try:
            import json
            token    = await self._token()
            parts    = remote_path.strip("/").rsplit("/", 1)
            filename = parts[-1]
            folder   = parts[0] if len(parts) > 1 else ""
            parent   = await self._ensure_folder(token, folder) if folder else "root"
            mime     = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            boundary = b"cs_boundary"
            meta     = json.dumps({"name": filename, "parents": [parent]}).encode()
            body = (
                b"--" + boundary + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + meta + b"\r\n--" + boundary + b"\r\nContent-Type: " + mime.encode() + b"\r\n\r\n"
                + data + b"\r\n--" + boundary + b"--"
            )
            r = await self._client.post(
                f"{_UPLOAD}?uploadType=multipart",
                content=body,
                headers={**self._auth(token), "Content-Type": f"multipart/related; boundary={boundary.decode()}"},
            )
            r.raise_for_status()
            return {"success": True, "path": remote_path, "size": len(data), "id": r.json().get("id")}
        except Exception as e:
            logger.error("google_drive(oauth).upload error: %s", e)
            return {"success": False, "error": str(e)}

    async def download(self, remote_path: str) -> bytes:
        token   = await self._token()
        file_id = await self._resolve_id(token, remote_path)
        if not file_id:
            raise FileNotFoundError(f"Google Drive: файл не найден: {remote_path}")
        r = await self._client.get(f"{_API}/files/{file_id}?alt=media", headers=self._auth(token))
        r.raise_for_status()
        return r.content

    async def list_files(self, remote_path: str = "") -> list[CloudFile]:
        try:
            token     = await self._token()
            parent_id = "root"
            if remote_path:
                resolved = await self._resolve_id(token, remote_path)
                if resolved:
                    parent_id = resolved
            r = await self._client.get(
                f"{_API}/files",
                params={"q": f"'{parent_id}' in parents and trashed=false",
                        "fields": "files(id,name,size,mimeType,modifiedTime)", "pageSize": 200},
                headers=self._auth(token),
            )
            r.raise_for_status()
            return [
                CloudFile(
                    name=f["name"], path=f"{remote_path.rstrip('/')}/{f['name']}",
                    size=int(f.get("size", 0)),
                    is_dir=f["mimeType"] == "application/vnd.google-apps.folder",
                    modified=f.get("modifiedTime", ""), extra={"id": f["id"]},
                )
                for f in r.json().get("files", [])
            ]
        except Exception as e:
            logger.error("google_drive(oauth).list_files error: %s", e)
            return []

    async def delete(self, remote_path: str) -> bool:
        try:
            token   = await self._token()
            file_id = await self._resolve_id(token, remote_path)
            if not file_id:
                return False
            r = await self._client.delete(f"{_API}/files/{file_id}", headers=self._auth(token))
            return r.status_code == 204
        except Exception as e:
            logger.error("google_drive(oauth).delete error: %s", e)
            return False

    async def _resolve_id(self, token: str, path: str) -> str | None:
        from urllib.parse import quote
        parts     = [p for p in path.strip("/").split("/") if p]
        parent_id = "root"
        for part in parts:
            q = f"name={quote(repr(part))} and '{parent_id}' in parents and trashed=false"
            r = await self._client.get(
                f"{_API}/files",
                params={"q": q, "fields": "files(id,mimeType)", "pageSize": 1},
                headers=self._auth(token),
            )
            r.raise_for_status()
            files = r.json().get("files", [])
            if not files:
                return None
            parent_id = files[0]["id"]
        return parent_id

    async def _ensure_folder(self, token: str, path: str) -> str:
        parts     = [p for p in path.strip("/").split("/") if p]
        parent_id = "root"
        for part in parts:
            from urllib.parse import quote
            q = f"name={quote(repr(part))} and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            r = await self._client.get(
                f"{_API}/files",
                params={"q": q, "fields": "files(id)", "pageSize": 1},
                headers=self._auth(token),
            )
            r.raise_for_status()
            files = r.json().get("files", [])
            if files:
                parent_id = files[0]["id"]
            else:
                cr = await self._client.post(
                    f"{_API}/files",
                    json={"name": part, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
                    headers=self._auth(token),
                )
                cr.raise_for_status()
                parent_id = cr.json()["id"]
        return parent_id

    async def close(self) -> None:
        await self._client.aclose()
