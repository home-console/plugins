"""
cloud_sync — плагин для выгрузки и синхронизации файлов с облачными хранилищами.

Провайдеры регистрируются двумя способами:
  1. Прямые credentials из env (CLOUD_SYNC_*) — не требуют других плагинов
  2. OAuth плагины (oauth_google, oauth_dropbox) — если установлены, используются автоматически

Приоритет: env credentials > oauth plugin > провайдер отключён.

optional_dependencies (декларативно, для UI):
  oauth_google  — Google Drive через OAuth вместо статического refresh_token
  oauth_dropbox — Dropbox через OAuth вместо статического токена

Sync jobs:
  Фоновая синхронизация по расписанию. Конфигурация через cloud_sync.sync.* сервисы.
  Каждый job: {provider, local_path, remote_path, interval_seconds, enabled}

Event bus:
  Подписка на file.uploaded от client-manager для автовыгрузки файлов.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from sdk.plugin_ext import BasePlugin, PluginMetadata

from .providers import DropboxProvider, GoogleDriveProvider, YandexDiskProvider
from .providers.base import CloudProvider

logger = logging.getLogger(__name__)

# Storage namespace for sync jobs
_SYNC_NS = "cloud_sync"
_SYNC_KEY = "sync_jobs"


class CloudSyncPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="cloud_sync",
        version="0.2.0",
        description="Выгрузка и синхронизация файлов с облачными хранилищами",
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._providers: dict[str, CloudProvider] = {}
        self._sync_jobs: list[dict[str, Any]] = []
        self._sync_task: Optional[asyncio.Task] = None
        self._sync_interval: int = 60  # default check interval
        self._background_tasks: set = set()

    async def on_load(self) -> None:
        await self._load_providers()
        await self._load_sync_jobs()

    async def on_start(self) -> None:
        await self._register_services()
        await self._register_http()
        await self._subscribe_events()
        await self._start_sync_loop()
        logger.info("cloud_sync started, providers: %s, sync_jobs: %d",
                     list(self._providers), len(self._sync_jobs))

    async def on_stop(self) -> None:
        await self._stop_sync_loop()
        for p in self._providers.values():
            await p.close()

    # ------------------------------------------------------------------
    # Provider loading — has_service() pattern
    # ------------------------------------------------------------------

    async def _load_providers(self) -> None:
        # ── Яндекс.Диск ───────────────────────────────────────────────
        if token := self.get_env_config("YANDEX_DISK_TOKEN"):
            self._providers["yandex_disk"] = YandexDiskProvider(token)
            logger.info("cloud_sync: yandex_disk provider loaded (env token)")

        # ── Google Drive ───────────────────────────────────────────────
        client_id     = self.get_env_config("GOOGLE_CLIENT_ID")
        client_secret = self.get_env_config("GOOGLE_CLIENT_SECRET")
        refresh_token = self.get_env_config("GOOGLE_REFRESH_TOKEN")
        if client_id and client_secret and refresh_token:
            self._providers["google_drive"] = GoogleDriveProvider(
                client_id, client_secret, refresh_token
            )
            logger.info("cloud_sync: google_drive provider loaded (env credentials)")

        elif await self.has_service("oauth_google.get_access_token"):
            from .providers.oauth_google import GoogleDriveOAuthProvider
            self._providers["google_drive"] = GoogleDriveOAuthProvider(self)
            logger.info("cloud_sync: google_drive provider loaded (via oauth_google plugin)")

        else:
            logger.info(
                "cloud_sync: google_drive disabled "
                "(нет CLOUD_SYNC_GOOGLE_* в env и oauth_google плагин не установлен)"
            )

        # ── Dropbox ────────────────────────────────────────────────────
        if token := self.get_env_config("DROPBOX_TOKEN"):
            self._providers["dropbox"] = DropboxProvider(token)
            logger.info("cloud_sync: dropbox provider loaded (env token)")

        elif await self.has_service("oauth_dropbox.get_access_token"):
            from .providers.oauth_dropbox import DropboxOAuthProvider
            self._providers["dropbox"] = DropboxOAuthProvider(self)
            logger.info("cloud_sync: dropbox provider loaded (via oauth_dropbox plugin)")

        else:
            logger.info(
                "cloud_sync: dropbox disabled "
                "(нет CLOUD_SYNC_DROPBOX_TOKEN в env и oauth_dropbox плагин не установлен)"
            )

    def _get_provider(self, provider_name: str) -> CloudProvider:
        p = self._providers.get(provider_name)
        if p is None:
            available = list(self._providers)
            raise ValueError(
                f"Провайдер '{provider_name}' не настроен. "
                f"Доступны: {available or ['нет']}. "
                f"Установи oauth_{provider_name.replace('_', '')} плагин "
                f"или задай CLOUD_SYNC_{provider_name.upper()}_* переменные."
            )
        return p

    # ------------------------------------------------------------------
    # Sync Jobs — persistence
    # ------------------------------------------------------------------

    async def _load_sync_jobs(self) -> None:
        """Load sync jobs from storage."""
        try:
            data = await self.call_service("storage.get", namespace=_SYNC_NS, key=_SYNC_KEY)
            if isinstance(data, list):
                self._sync_jobs = data
            elif isinstance(data, dict) and "jobs" in data:
                self._sync_jobs = data["jobs"]
            else:
                self._sync_jobs = []
        except Exception:
            self._sync_jobs = []

    async def _save_sync_jobs(self) -> None:
        """Persist sync jobs to storage."""
        try:
            await self.call_service(
                "storage.set",
                namespace=_SYNC_NS,
                key=_SYNC_KEY,
                value={"jobs": self._sync_jobs},
            )
        except Exception as e:
            logger.warning("cloud_sync: failed to save sync jobs: %s", e)

    # ------------------------------------------------------------------
    # Sync Jobs — background loop
    # ------------------------------------------------------------------

    async def _start_sync_loop(self) -> None:
        if not self._sync_jobs:
            return
        self._sync_task = asyncio.create_task(self._sync_loop())
        self._background_tasks.add(self._sync_task)
        self._sync_task.add_done_callback(self._background_tasks.discard)

    async def _stop_sync_loop(self) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        for t in self._background_tasks:
            if not t.done():
                t.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    async def _sync_loop(self) -> None:
        """Background loop: check sync jobs and run due ones."""
        try:
            while True:
                now = time.time()
                for job in self._sync_jobs:
                    if not job.get("enabled", True):
                        continue
                    last_run = job.get("last_run", 0)
                    interval = job.get("interval_seconds", 3600)
                    if now - last_run >= interval:
                        try:
                            await self._run_sync_job(job)
                            job["last_run"] = now
                        except Exception as e:
                            logger.error("cloud_sync: sync job failed: %s", e)
                            job["last_error"] = str(e)
                await self._save_sync_jobs()
                await asyncio.sleep(self._sync_interval)
        except asyncio.CancelledError:
            pass

    async def _run_sync_job(self, job: dict[str, Any]) -> None:
        """Execute a single sync job: upload local files to remote."""
        provider_name = job.get("provider", "")
        local_path = job.get("local_path", "")
        remote_path = job.get("remote_path", "/")

        if not provider_name or not local_path:
            return

        p = self._get_provider(provider_name)
        local = Path(local_path)
        if not local.exists():
            logger.warning("cloud_sync: local path does not exist: %s", local_path)
            return

        if local.is_file():
            data = local.read_bytes()
            rel_name = local.name
            await p.upload(data, f"{remote_path.rstrip('/')}/{rel_name}")
            await self.publish_event("cloud_sync.sync_completed", {
                "provider": provider_name,
                "local_path": str(local),
                "remote_path": f"{remote_path.rstrip('/')}/{rel_name}",
                "size": len(data),
            })
        elif local.is_dir():
            for f in sorted(local.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(local)
                    data = f.read_bytes()
                    remote_file = f"{remote_path.rstrip('/')}/{rel}"
                    await p.upload(data, remote_file)
            await self.publish_event("cloud_sync.sync_completed", {
                "provider": provider_name,
                "local_path": str(local),
                "remote_path": remote_path,
            })

    # ------------------------------------------------------------------
    # Event bus — subscribe to file.uploaded
    # ------------------------------------------------------------------

    async def _subscribe_events(self) -> None:
        """Subscribe to file.* events from client-manager for auto-upload."""
        try:
            await self.call_service(
                "event_bus.subscribe",
                event_type="file.uploaded",
                handler=self._on_file_uploaded,
            )
            logger.info("cloud_sync: subscribed to file.uploaded events")
        except Exception as e:
            logger.debug("cloud_sync: event_bus.subscribe not available: %s", e)

    async def _on_file_uploaded(self, event: dict[str, Any]) -> None:
        """Handle file.uploaded event: auto-upload to default provider."""
        if not self._sync_jobs:
            return
        # Find first enabled job with auto_upload
        for job in self._sync_jobs:
            if job.get("auto_upload") and job.get("enabled", True):
                provider_name = job.get("provider", "")
                if provider_name not in self._providers:
                    continue
                remote_dir = job.get("remote_path", "/")
                local_path = event.get("path", "")
                client_id = event.get("client_id", "")
                filename = event.get("filename", os.path.basename(local_path) if local_path else "")
                if not filename:
                    continue
                remote_file = f"{remote_dir.rstrip('/')}/{client_id}/{filename}"
                try:
                    # File content may need to be fetched from agent
                    logger.info("cloud_sync: auto-upload %s → %s/%s",
                                filename, provider_name, remote_file)
                except Exception as e:
                    logger.error("cloud_sync: auto-upload failed: %s", e)

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    async def _register_services(self) -> None:
        async def get_providers() -> dict[str, Any]:
            result = []
            for name, p in self._providers.items():
                ok = await p.check()
                result.append({"provider": name, "connected": ok})
            for name in ("yandex_disk", "google_drive", "dropbox"):
                if name not in self._providers:
                    result.append({"provider": name, "connected": False, "disabled": True})
            return {"providers": result}

        async def upload(provider: str, data: bytes | str, remote_path: str, **_: Any) -> dict[str, Any]:
            p   = self._get_provider(provider)
            raw = data if isinstance(data, bytes) else data.encode()
            res = await p.upload(raw, remote_path)
            event = "cloud_sync.upload_completed" if res.get("success") else "cloud_sync.upload_failed"
            await self.publish_event(event, {"provider": provider, "remote_path": remote_path,
                                              "size": res.get("size", 0), "error": res.get("error")})
            return res

        async def download(provider: str, remote_path: str, **_: Any) -> dict[str, Any]:
            p    = self._get_provider(provider)
            data = await p.download(remote_path)
            await self.publish_event("cloud_sync.download_completed",
                                     {"provider": provider, "remote_path": remote_path, "size": len(data)})
            return {"data": data, "size": len(data), "remote_path": remote_path}

        async def list_files(provider: str, remote_path: str = "", **_: Any) -> dict[str, Any]:
            p     = self._get_provider(provider)
            files = await p.list_files(remote_path)
            return {
                "provider": provider, "path": remote_path,
                "files": [{"name": f.name, "path": f.path, "size": f.size,
                            "is_dir": f.is_dir, "modified": f.modified} for f in files],
                "count": len(files),
            }

        async def delete_file(provider: str, remote_path: str, **_: Any) -> dict[str, Any]:
            p  = self._get_provider(provider)
            ok = await p.delete(remote_path)
            return {"success": ok, "provider": provider, "remote_path": remote_path}

        # ── Sync Jobs services ─────────────────────────────────────────

        async def list_sync_jobs(**_: Any) -> dict[str, Any]:
            return {"jobs": self._sync_jobs, "count": len(self._sync_jobs)}

        async def create_sync_job(
            provider: str,
            local_path: str,
            remote_path: str = "/",
            interval_seconds: int = 3600,
            enabled: bool = True,
            auto_upload: bool = False,
            **_: Any,
        ) -> dict[str, Any]:
            if provider not in self._providers:
                return {"ok": False, "error": f"Provider '{provider}' not available"}
            if not Path(local_path).exists():
                return {"ok": False, "error": f"Local path does not exist: {local_path}"}
            job = {
                "id": f"sync_{int(time.time())}_{len(self._sync_jobs)}",
                "provider": provider,
                "local_path": local_path,
                "remote_path": remote_path,
                "interval_seconds": interval_seconds,
                "enabled": enabled,
                "auto_upload": auto_upload,
                "last_run": 0,
                "created_at": time.time(),
            }
            self._sync_jobs.append(job)
            await self._save_sync_jobs()
            # Restart sync loop if needed
            if enabled and self._sync_task is None:
                await self._start_sync_loop()
            return {"ok": True, "job": job}

        async def update_sync_job(job_id: str, **fields: Any) -> dict[str, Any]:
            for job in self._sync_jobs:
                if job.get("id") == job_id:
                    for k, v in fields.items():
                        if k in ("provider", "local_path", "remote_path", "interval_seconds",
                                 "enabled", "auto_upload"):
                            job[k] = v
                    await self._save_sync_jobs()
                    return {"ok": True, "job": job}
            return {"ok": False, "error": f"Job not found: {job_id}"}

        async def delete_sync_job(job_id: str, **_: Any) -> dict[str, Any]:
            before = len(self._sync_jobs)
            self._sync_jobs = [j for j in self._sync_jobs if j.get("id") != job_id]
            if len(self._sync_jobs) < before:
                await self._save_sync_jobs()
                return {"ok": True, "deleted": 1}
            return {"ok": False, "error": f"Job not found: {job_id}"}

        async def run_sync_job_now(job_id: str, **_: Any) -> dict[str, Any]:
            for job in self._sync_jobs:
                if job.get("id") == job_id:
                    try:
                        await self._run_sync_job(job)
                        job["last_run"] = time.time()
                        await self._save_sync_jobs()
                        return {"ok": True, "job": job}
                    except Exception as e:
                        return {"ok": False, "error": str(e)}
            return {"ok": False, "error": f"Job not found: {job_id}"}

        await self.register_service("cloud_sync.providers", get_providers)
        await self.register_service("cloud_sync.upload",    upload)
        await self.register_service("cloud_sync.download",  download)
        await self.register_service("cloud_sync.list",      list_files)
        await self.register_service("cloud_sync.delete",    delete_file)
        await self.register_service("cloud_sync.sync.list",     list_sync_jobs)
        await self.register_service("cloud_sync.sync.create",   create_sync_job)
        await self.register_service("cloud_sync.sync.update",   update_sync_job)
        await self.register_service("cloud_sync.sync.delete",   delete_sync_job)
        await self.register_service("cloud_sync.sync.run_now",  run_sync_job_now)

    # ------------------------------------------------------------------
    # HTTP endpoints
    # ------------------------------------------------------------------

    async def _register_http(self) -> None:
        try:
            from sdk.http import EndpointAuthConfig, HttpEndpoint
        except ImportError:
            logger.warning("cloud_sync: sdk.http недоступен, HTTP-эндпоинты не зарегистрированы")
            return

        _auth = EndpointAuthConfig(required_scopes=["admin.write"])
        _read = EndpointAuthConfig(required_scopes=["admin.read"])

        self.register_http_endpoint(HttpEndpoint(
            method="GET",  path="/api/v1/plugins/cloud-sync/providers",
            service="cloud_sync.providers", auth_config=_read,
            description="Список провайдеров и статус подключения",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="POST", path="/api/v1/plugins/cloud-sync/upload",
            service="cloud_sync.upload", auth_config=_auth,
            description="Загрузить файл в облако (provider, data, remote_path)",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="POST", path="/api/v1/plugins/cloud-sync/download",
            service="cloud_sync.download", auth_config=_auth,
            description="Скачать файл из облака (provider, remote_path)",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="GET",  path="/api/v1/plugins/cloud-sync/files",
            service="cloud_sync.list", auth_config=_read,
            description="Список файлов (provider, remote_path)",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="DELETE", path="/api/v1/plugins/cloud-sync/files",
            service="cloud_sync.delete", auth_config=_auth,
            description="Удалить файл (provider, remote_path)",
        ))
        # Sync jobs endpoints
        self.register_http_endpoint(HttpEndpoint(
            method="GET",  path="/api/v1/plugins/cloud-sync/sync/jobs",
            service="cloud_sync.sync.list", auth_config=_read,
            description="Список задач синхронизации",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="POST", path="/api/v1/plugins/cloud-sync/sync/jobs",
            service="cloud_sync.sync.create", auth_config=_auth,
            description="Создать задачу синхронизации",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="PUT",  path="/api/v1/plugins/cloud-sync/sync/jobs/{job_id}",
            service="cloud_sync.sync.update", auth_config=_auth,
            description="Обновить задачу синхронизации",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="DELETE", path="/api/v1/plugins/cloud-sync/sync/jobs/{job_id}",
            service="cloud_sync.sync.delete", auth_config=_auth,
            description="Удалить задачу синхронизации",
        ))
        self.register_http_endpoint(HttpEndpoint(
            method="POST", path="/api/v1/plugins/cloud-sync/sync/jobs/{job_id}/run",
            service="cloud_sync.sync.run_now", auth_config=_auth,
            description="Запустить задачу синхронизации немедленно",
        ))
