"""
TrueNAS Scale плагин для HomeConsole Core.

Управляет пулами, датасетами, снапшотами и шарами через TrueNAS REST API v2.

Конфиг через env:
  TRUENAS_HOST       — hostname или IP TrueNAS (обязательно), напр. 192.168.1.200
  TRUENAS_PORT       — порт HTTPS (по умолчанию 443)
  TRUENAS_API_KEY    — API ключ из TrueNAS UI → Settings → API Keys
  TRUENAS_VERIFY_SSL — проверять SSL (по умолчанию false для self-signed)

Сервисы:
  truenas.system.info()                           → версия, hostname, uptime
  truenas.pool.list()                             → ZFS пулы
  truenas.dataset.list(pool)                      → датасеты пула
  truenas.dataset.create(name, pool, type)        → создать датасет
  truenas.dataset.delete(id, recursive)           → удалить датасет
  truenas.snapshot.list(dataset)                  → снапшоты
  truenas.snapshot.create(dataset, name)          → создать снапшот
  truenas.snapshot.delete(id)                     → удалить снапшот
  truenas.share.smb.list()                        → SMB-шары
  truenas.share.nfs.list()                        → NFS-шары
  truenas.alert.list()                            → активные алерты
  truenas.status()                                → статус подключения
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from sdk.plugin_ext import BasePlugin, PluginMetadata

logger = logging.getLogger(__name__)


class TrueNASPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="truenas",
        version="0.1.0",
        description="TrueNAS Scale: управление пулами, датасетами, снапшотами через REST API",
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._host       = ""
        self._port       = 443
        self._api_key    = ""
        self._verify_ssl = False

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_load(self) -> None:
        self._host       = self.get_env_config("TRUENAS_HOST", "")
        self._port       = self.get_env_config_int("TRUENAS_PORT", 443)
        self._api_key    = self.get_env_config("TRUENAS_API_KEY", "")
        self._verify_ssl = self.get_env_config_bool("TRUENAS_VERIFY_SSL", False)

        if not self._host:
            logger.warning("truenas: TRUENAS_HOST не задан — плагин загружен, но API недоступен")
        if not self._api_key:
            logger.warning("truenas: TRUENAS_API_KEY не задан")

        await self._register_services()
        await self._register_http()

    # ------------------------------------------------------------------
    # HTTP client

    def _base_url(self) -> str:
        return f"https://{self._host}:{self._port}/api/v2.0"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=20.0,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def _get(self, path: str, **params: Any) -> Any:
        if not self._host:
            raise RuntimeError("TRUENAS_HOST не задан")
        async with self._client() as c:
            r = await c.get(
                f"{self._base_url()}{path}",
                params={k: v for k, v in params.items() if v is not None},
            )
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: Any = None) -> Any:
        if not self._host:
            raise RuntimeError("TRUENAS_HOST не задан")
        async with self._client() as c:
            r = await c.post(f"{self._base_url()}{path}", json=body)
            r.raise_for_status()
            return r.json()

    async def _delete(self, path: str, body: Any = None) -> Any:
        if not self._host:
            raise RuntimeError("TRUENAS_HOST не задан")
        async with self._client() as c:
            r = await c.delete(f"{self._base_url()}{path}", json=body)
            r.raise_for_status()
            return r.json() if r.content else None

    # ------------------------------------------------------------------
    # Services

    async def _register_services(self) -> None:

        async def system_info(**_: Any) -> dict[str, Any]:
            data = await self._get("/system/info")
            return {"ok": True, "info": data}

        async def pool_list(**_: Any) -> dict[str, Any]:
            data = await self._get("/pool")
            return {"ok": True, "pools": data or [], "total": len(data or [])}

        async def dataset_list(pool: str = "", **_: Any) -> dict[str, Any]:
            params = {}
            if pool:
                params["pool"] = pool
            data = await self._get("/pool/dataset", **params)
            return {"ok": True, "datasets": data or [], "total": len(data or [])}

        async def dataset_create(
            name: str,
            pool: str = "",
            dataset_type: str = "FILESYSTEM",
            comments: str = "",
            **_: Any,
        ) -> dict[str, Any]:
            full_name = f"{pool}/{name}" if pool and "/" not in name else name
            body = {"name": full_name, "type": dataset_type}
            if comments:
                body["comments"] = comments
            data = await self._post("/pool/dataset", body)
            await self.publish_event("truenas.dataset.created", {"name": full_name, "pool": pool})
            return {"ok": True, "dataset": data}

        async def dataset_delete(
            dataset_id: str,
            recursive: bool = False,
            **_: Any,
        ) -> dict[str, Any]:
            await self._delete(f"/pool/dataset/id/{dataset_id}", {"recursive": recursive, "force": False})
            return {"ok": True, "dataset_id": dataset_id}

        async def snapshot_list(dataset: str = "", **_: Any) -> dict[str, Any]:
            params = {"dataset": dataset} if dataset else {}
            data = await self._get("/zfs/snapshot", **params)
            return {"ok": True, "snapshots": data or [], "total": len(data or [])}

        async def snapshot_create(
            dataset: str,
            name: str = "",
            recursive: bool = False,
            **_: Any,
        ) -> dict[str, Any]:
            import time as _time
            snap_name = name or _time.strftime("auto-%Y%m%d-%H%M%S")
            body = {"dataset": dataset, "name": snap_name, "recursive": recursive}
            data = await self._post("/zfs/snapshot", body)
            await self.publish_event("truenas.snapshot.created", {
                "dataset": dataset, "name": snap_name, "id": f"{dataset}@{snap_name}",
            })
            return {"ok": True, "snapshot": data, "id": f"{dataset}@{snap_name}"}

        async def snapshot_delete(snapshot_id: str, **_: Any) -> dict[str, Any]:
            await self._delete(f"/zfs/snapshot/id/{snapshot_id}")
            return {"ok": True, "snapshot_id": snapshot_id}

        async def share_smb_list(**_: Any) -> dict[str, Any]:
            data = await self._get("/sharing/smb")
            return {"ok": True, "shares": data or [], "total": len(data or [])}

        async def share_nfs_list(**_: Any) -> dict[str, Any]:
            data = await self._get("/sharing/nfs")
            return {"ok": True, "shares": data or [], "total": len(data or [])}

        async def alert_list(**_: Any) -> dict[str, Any]:
            data = await self._get("/alert/list")
            return {"ok": True, "alerts": data or [], "total": len(data or [])}

        async def status(**_: Any) -> dict[str, Any]:
            if not self._host or not self._api_key:
                return {"ok": False, "configured": False, "error": "TRUENAS_HOST / TRUENAS_API_KEY не заданы"}
            try:
                info = await self._get("/system/info")
                return {
                    "ok": True, "configured": True,
                    "hostname": info.get("hostname"),
                    "version":  info.get("version"),
                    "host":     self._host,
                }
            except Exception as e:
                return {"ok": False, "configured": True, "error": str(e), "host": self._host}

        await self.register_service("truenas.system.info",      system_info)
        await self.register_service("truenas.pool.list",        pool_list)
        await self.register_service("truenas.dataset.list",     dataset_list)
        await self.register_service("truenas.dataset.create",   dataset_create)
        await self.register_service("truenas.dataset.delete",   dataset_delete)
        await self.register_service("truenas.snapshot.list",    snapshot_list)
        await self.register_service("truenas.snapshot.create",  snapshot_create)
        await self.register_service("truenas.snapshot.delete",  snapshot_delete)
        await self.register_service("truenas.share.smb.list",   share_smb_list)
        await self.register_service("truenas.share.nfs.list",   share_nfs_list)
        await self.register_service("truenas.alert.list",       alert_list)
        await self.register_service("truenas.status",           status)

    # ------------------------------------------------------------------
    # HTTP endpoints

    async def _register_http(self) -> None:
        try:
            from sdk.http import EndpointAuthConfig, HttpEndpoint
        except ImportError:
            return

        _r = EndpointAuthConfig(required_scopes=["admin.read"])
        _w = EndpointAuthConfig(required_scopes=["admin.write"])
        b  = "/api/v1/plugins/truenas"

        for ep in [
            HttpEndpoint(method="GET",    path=f"{b}/status",                       service="truenas.status",          description="Статус подключения",         auth_config=_r),
            HttpEndpoint(method="GET",    path=f"{b}/system",                       service="truenas.system.info",     description="Информация о системе",       auth_config=_r),
            HttpEndpoint(method="GET",    path=f"{b}/pools",                        service="truenas.pool.list",       description="Список ZFS-пулов",           auth_config=_r),
            HttpEndpoint(method="GET",    path=f"{b}/datasets",                     service="truenas.dataset.list",    description="Список датасетов",           auth_config=_r),
            HttpEndpoint(method="POST",   path=f"{b}/datasets",                     service="truenas.dataset.create",  description="Создать датасет",            auth_config=_w),
            HttpEndpoint(method="DELETE", path=f"{b}/datasets/{{dataset_id}}",      service="truenas.dataset.delete",  description="Удалить датасет",            auth_config=_w),
            HttpEndpoint(method="GET",    path=f"{b}/snapshots",                    service="truenas.snapshot.list",   description="Список снапшотов",           auth_config=_r),
            HttpEndpoint(method="POST",   path=f"{b}/snapshots",                    service="truenas.snapshot.create", description="Создать снапшот",            auth_config=_w),
            HttpEndpoint(method="DELETE", path=f"{b}/snapshots/{{snapshot_id}}",    service="truenas.snapshot.delete", description="Удалить снапшот",            auth_config=_w),
            HttpEndpoint(method="GET",    path=f"{b}/shares/smb",                   service="truenas.share.smb.list",  description="SMB-шары",                   auth_config=_r),
            HttpEndpoint(method="GET",    path=f"{b}/shares/nfs",                   service="truenas.share.nfs.list",  description="NFS-шары",                   auth_config=_r),
            HttpEndpoint(method="GET",    path=f"{b}/alerts",                       service="truenas.alert.list",      description="Активные алерты TrueNAS",    auth_config=_r),
        ]:
            self.register_http_endpoint(ep)
