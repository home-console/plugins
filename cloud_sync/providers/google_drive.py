from __future__ import annotations

import json
import logging
import mimetypes
import time
from typing import Any
from urllib.parse import quote

import httpx

from .base import CloudFile, CloudProvider

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API = "https://www.googleapis.com/drive/v3"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"


class GoogleDriveProvider(CloudProvider):
    name = "google_drive"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str = ""
        self._token_expires: float = 0.0
        self._client = httpx.AsyncClient(timeout=60.0)

    async def _ensure_token(self) -> None:
        if self._access_token and time.time() < self._token_expires - 60:
            return
        r = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
        )
        r.raise_for_status()
        body = r.json()
        self._access_token = body["access_token"]
        self._token_expires = time.time() + body.get("expires_in", 3600)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def check(self) -> bool:
        try:
            await self._ensure_token()
            r = await self._client.get(f"{_API}/about?fields=user", headers=self._auth())
            return r.status_code == 200
        except Exception as e:
            logger.warning("google_drive.check failed: %s", e)
            return False

    async def _resolve_path_to_id(self, path: str) -> str | None:
        """Преобразует unix-путь (/folder/sub/file.txt) в Drive file ID."""
        parts = [p for p in path.strip("/").split("/") if p]
        parent_id = "root"
        for part in parts:
            q = f"name={quote(repr(part))} and '{parent_id}' in parents and trashed=false"
            r = await self._client.get(
                f"{_API}/files",
                params={"q": q, "fields": "files(id,name,mimeType)", "pageSize": 1},
                headers=self._auth(),
            )
            r.raise_for_status()
            files = r.json().get("files", [])
            if not files:
                return None
            parent_id = files[0]["id"]
        return parent_id

    async def _ensure_folder(self, path: str) -> str:
        """Создаёт папки по пути если их нет, возвращает ID конечной папки."""
        parts = [p for p in path.strip("/").split("/") if p]
        parent_id = "root"
        for part in parts:
            q = f"name={quote(repr(part))} and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            r = await self._client.get(
                f"{_API}/files",
                params={"q": q, "fields": "files(id)", "pageSize": 1},
                headers=self._auth(),
            )
            r.raise_for_status()
            files = r.json().get("files", [])
            if files:
                parent_id = files[0]["id"]
            else:
                cr = await self._client.post(
                    f"{_API}/files",
                    json={
                        "name": part,
                        "mimeType": "application/vnd.google-apps.folder",
                        "parents": [parent_id],
                    },
                    headers=self._auth(),
                )
                cr.raise_for_status()
                parent_id = cr.json()["id"]
        return parent_id

    async def upload(self, data: bytes, remote_path: str) -> dict[str, Any]:
        try:
            await self._ensure_token()
            path = remote_path.strip("/")
            parts = path.rsplit("/", 1)
            filename = parts[-1]
            folder_path = parts[0] if len(parts) > 1 else ""

            parent_id = await self._ensure_folder(folder_path) if folder_path else "root"
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

            # Multipart upload
            metadata = json.dumps({"name": filename, "parents": [parent_id]}).encode()
            boundary = b"cloudsync_boundary"
            body = (
                b"--" + boundary + b"\r\n"
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                + metadata + b"\r\n"
                b"--" + boundary + b"\r\n"
                + f"Content-Type: {mime}\r\n\r\n".encode()
                + data + b"\r\n"
                b"--" + boundary + b"--"
            )
            r = await self._client.post(
                f"{_UPLOAD}?uploadType=multipart",
                content=body,
                headers={
                    **self._auth(),
                    "Content-Type": f"multipart/related; boundary={boundary.decode()}",
                },
            )
            r.raise_for_status()
            return {"success": True, "path": remote_path, "size": len(data), "id": r.json().get("id")}
        except Exception as e:
            logger.error("google_drive.upload error: %s", e)
            return {"success": False, "error": str(e)}

    async def download(self, remote_path: str) -> bytes:
        await self._ensure_token()
        file_id = await self._resolve_path_to_id(remote_path)
        if not file_id:
            raise FileNotFoundError(f"Google Drive: файл не найден: {remote_path}")
        r = await self._client.get(
            f"{_API}/files/{file_id}?alt=media",
            headers=self._auth(),
        )
        r.raise_for_status()
        return r.content

    async def list_files(self, remote_path: str = "") -> list[CloudFile]:
        try:
            await self._ensure_token()
            parent_id = "root"
            if remote_path:
                resolved = await self._resolve_path_to_id(remote_path)
                if resolved:
                    parent_id = resolved

            q = f"'{parent_id}' in parents and trashed=false"
            r = await self._client.get(
                f"{_API}/files",
                params={
                    "q": q,
                    "fields": "files(id,name,size,mimeType,modifiedTime)",
                    "pageSize": 200,
                },
                headers=self._auth(),
            )
            r.raise_for_status()
            files = r.json().get("files", [])
            return [
                CloudFile(
                    name=f["name"],
                    path=f"{remote_path.rstrip('/')}/{f['name']}",
                    size=int(f.get("size", 0)),
                    is_dir=f["mimeType"] == "application/vnd.google-apps.folder",
                    modified=f.get("modifiedTime", ""),
                    mime_type=f.get("mimeType", ""),
                    extra={"id": f["id"]},
                )
                for f in files
            ]
        except Exception as e:
            logger.error("google_drive.list_files error: %s", e)
            return []

    async def delete(self, remote_path: str) -> bool:
        try:
            await self._ensure_token()
            file_id = await self._resolve_path_to_id(remote_path)
            if not file_id:
                return False
            r = await self._client.delete(
                f"{_API}/files/{file_id}",
                headers=self._auth(),
            )
            return r.status_code == 204
        except Exception as e:
            logger.error("google_drive.delete error: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
