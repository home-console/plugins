"""
Proxmox VE плагин для HomeConsole Core.

Управляет виртуальными машинами и LXC-контейнерами через Proxmox REST API.
Также регистрирует ProxmoxCommandAdapter в client-manager для агентов с pvesh.

Конфиг через env:
  PROXMOX_HOST        — hostname или IP Proxmox (обязательно), напр. 192.168.1.100
  PROXMOX_PORT        — порт API (по умолчанию 8006)
  PROXMOX_TOKEN_ID    — API token ID, напр. root@pam!mytoken
  PROXMOX_TOKEN_SECRET — API token secret
  PROXMOX_NODE        — имя PVE-узла (по умолчанию pve)
  PROXMOX_VERIFY_SSL  — проверять SSL (по умолчанию false для self-signed)

Сервисы:
  proxmox.node.list()                        → список PVE-узлов
  proxmox.node.status(node)                  → статус узла (CPU, RAM, uptime)
  proxmox.vm.list(node)                      → все VM
  proxmox.vm.start(vmid, node)               → запустить VM
  proxmox.vm.stop(vmid, node)                → остановить VM
  proxmox.vm.reboot(vmid, node)              → перезагрузить VM
  proxmox.vm.status(vmid, node)              → статус VM
  proxmox.ct.list(node)                      → все LXC-контейнеры
  proxmox.ct.start(ctid, node)               → запустить контейнер
  proxmox.ct.stop(ctid, node)                → остановить контейнер
  proxmox.storage.list(node)                 → хранилища
  proxmox.backup.list(node, vmid)            → бэкапы
  proxmox.status()                           → общий статус подключения
"""
from __future__ import annotations

import logging
import ssl
from typing import Any

import httpx

from sdk.plugin_ext import BasePlugin, PluginMetadata

logger = logging.getLogger(__name__)


class ProxmoxPlugin(BasePlugin):
    metadata = PluginMetadata(
        name="proxmox",
        version="0.1.0",
        description="Proxmox VE: управление VM и LXC через REST API",
    )

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._host        = ""
        self._port        = 8006
        self._token_id    = ""
        self._token_secret = ""
        self._node        = "pve"
        self._verify_ssl  = False

    # ------------------------------------------------------------------
    # Lifecycle

    async def on_load(self) -> None:
        self._host         = self.get_env_config("PROXMOX_HOST", "")
        self._port         = self.get_env_config_int("PROXMOX_PORT", 8006)
        self._token_id     = self.get_env_config("PROXMOX_TOKEN_ID", "")
        self._token_secret = self.get_env_config("PROXMOX_TOKEN_SECRET", "")
        self._node         = self.get_env_config("PROXMOX_NODE", "pve")
        self._verify_ssl   = self.get_env_config_bool("PROXMOX_VERIFY_SSL", False)

        if not self._host:
            logger.warning("proxmox: PROXMOX_HOST не задан — плагин загружен, но API недоступен")
        if not self._token_id or not self._token_secret:
            logger.warning("proxmox: PROXMOX_TOKEN_ID / PROXMOX_TOKEN_SECRET не заданы")

        await self._register_services()
        await self._register_http()

    async def on_start(self) -> None:
        # Регистрируем CommandAdapter в client-manager (если он запущен)
        await self._register_command_adapter()

    # ------------------------------------------------------------------
    # HTTP client

    def _base_url(self) -> str:
        return f"https://{self._host}:{self._port}/api2/json"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"PVEAPIToken={self._token_id}={self._token_secret}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=15.0,
            headers=self._headers(),
        )

    async def _get(self, path: str, **params: Any) -> Any:
        if not self._host:
            raise RuntimeError("PROXMOX_HOST не задан")
        async with self._client() as c:
            r = await c.get(f"{self._base_url()}{path}", params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json().get("data")

    async def _post(self, path: str, **body: Any) -> Any:
        if not self._host:
            raise RuntimeError("PROXMOX_HOST не задан")
        async with self._client() as c:
            r = await c.post(f"{self._base_url()}{path}", json={k: v for k, v in body.items() if v is not None})
            r.raise_for_status()
            return r.json().get("data")

    # ------------------------------------------------------------------
    # CommandAdapter registration

    async def _register_command_adapter(self) -> None:
        try:
            from .adapter import ProxmoxCommandAdapter
            await self.call_service(
                "client_manager.commands.register",
                device_type="proxmox",
                adapter=ProxmoxCommandAdapter(),
            )
            logger.info("proxmox: ProxmoxCommandAdapter зарегистрирован для device_type=proxmox")
        except Exception as e:
            logger.debug("proxmox: не удалось зарегистрировать CommandAdapter: %s", e)

    # ------------------------------------------------------------------
    # Services

    async def _register_services(self) -> None:
        node = self._node

        async def node_list(**_: Any) -> dict[str, Any]:
            data = await self._get("/nodes")
            return {"ok": True, "nodes": data or []}

        async def node_status(node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            data = await self._get(f"/nodes/{n}/status")
            return {"ok": True, "node": n, "status": data}

        async def vm_list(node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            data = await self._get(f"/nodes/{n}/qemu")
            vms = sorted(data or [], key=lambda x: x.get("vmid", 0))
            return {"ok": True, "node": n, "vms": vms, "total": len(vms)}

        async def vm_start(vmid: int, node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            task = await self._post(f"/nodes/{n}/qemu/{vmid}/status/start")
            await self.publish_event("proxmox.vm.started", {"vmid": vmid, "node": n, "task": task})
            return {"ok": True, "vmid": vmid, "node": n, "task": task}

        async def vm_stop(vmid: int, node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            task = await self._post(f"/nodes/{n}/qemu/{vmid}/status/stop")
            await self.publish_event("proxmox.vm.stopped", {"vmid": vmid, "node": n, "task": task})
            return {"ok": True, "vmid": vmid, "node": n, "task": task}

        async def vm_reboot(vmid: int, node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            task = await self._post(f"/nodes/{n}/qemu/{vmid}/status/reboot")
            return {"ok": True, "vmid": vmid, "node": n, "task": task}

        async def vm_status(vmid: int, node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            data = await self._get(f"/nodes/{n}/qemu/{vmid}/status/current")
            return {"ok": True, "vmid": vmid, "node": n, "status": data}

        async def ct_list(node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            data = await self._get(f"/nodes/{n}/lxc")
            cts = sorted(data or [], key=lambda x: x.get("vmid", 0))
            return {"ok": True, "node": n, "containers": cts, "total": len(cts)}

        async def ct_start(ctid: int, node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            task = await self._post(f"/nodes/{n}/lxc/{ctid}/status/start")
            await self.publish_event("proxmox.ct.started", {"ctid": ctid, "node": n, "task": task})
            return {"ok": True, "ctid": ctid, "node": n, "task": task}

        async def ct_stop(ctid: int, node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            task = await self._post(f"/nodes/{n}/lxc/{ctid}/status/stop")
            await self.publish_event("proxmox.ct.stopped", {"ctid": ctid, "node": n, "task": task})
            return {"ok": True, "ctid": ctid, "node": n, "task": task}

        async def storage_list(node_name: str = "", **_: Any) -> dict[str, Any]:
            n = node_name or node
            data = await self._get(f"/nodes/{n}/storage")
            return {"ok": True, "node": n, "storages": data or []}

        async def backup_list(node_name: str = "", vmid: int | None = None, **_: Any) -> dict[str, Any]:
            n = node_name or node
            params = {"vmid": vmid} if vmid else {}
            data = await self._get(f"/nodes/{n}/storage/local/content", **params)
            backups = [f for f in (data or []) if f.get("content") == "backup"]
            return {"ok": True, "node": n, "backups": backups}

        async def status(**_: Any) -> dict[str, Any]:
            configured = bool(self._host and self._token_id)
            if not configured:
                return {"ok": False, "configured": False, "error": "PROXMOX_HOST / PROXMOX_TOKEN_ID не заданы"}
            try:
                data = await self._get("/nodes")
                return {"ok": True, "configured": True, "nodes": len(data or []), "host": self._host}
            except Exception as e:
                return {"ok": False, "configured": True, "error": str(e), "host": self._host}

        await self.register_service("proxmox.node.list",    node_list)
        await self.register_service("proxmox.node.status",  node_status)
        await self.register_service("proxmox.vm.list",      vm_list)
        await self.register_service("proxmox.vm.start",     vm_start)
        await self.register_service("proxmox.vm.stop",      vm_stop)
        await self.register_service("proxmox.vm.reboot",    vm_reboot)
        await self.register_service("proxmox.vm.status",    vm_status)
        await self.register_service("proxmox.ct.list",      ct_list)
        await self.register_service("proxmox.ct.start",     ct_start)
        await self.register_service("proxmox.ct.stop",      ct_stop)
        await self.register_service("proxmox.storage.list", storage_list)
        await self.register_service("proxmox.backup.list",  backup_list)
        await self.register_service("proxmox.status",       status)

    # ------------------------------------------------------------------
    # HTTP endpoints

    async def _register_http(self) -> None:
        try:
            from sdk.http import EndpointAuthConfig, HttpEndpoint
        except ImportError:
            return

        _r = EndpointAuthConfig(required_scopes=["admin.read"])
        _w = EndpointAuthConfig(required_scopes=["admin.write"])
        b  = "/api/v1/plugins/proxmox"

        for ep in [
            HttpEndpoint(method="GET",  path=f"{b}/status",                   service="proxmox.status",       description="Статус подключения к Proxmox",    auth_config=_r),
            HttpEndpoint(method="GET",  path=f"{b}/nodes",                    service="proxmox.node.list",    description="Список PVE-узлов",                auth_config=_r),
            HttpEndpoint(method="GET",  path=f"{b}/nodes/{{node}}/status",    service="proxmox.node.status",  description="Статус узла",                    auth_config=_r),
            HttpEndpoint(method="GET",  path=f"{b}/vms",                      service="proxmox.vm.list",      description="Список всех VM",                  auth_config=_r),
            HttpEndpoint(method="POST", path=f"{b}/vms/{{vmid}}/start",       service="proxmox.vm.start",     description="Запустить VM",                   auth_config=_w),
            HttpEndpoint(method="POST", path=f"{b}/vms/{{vmid}}/stop",        service="proxmox.vm.stop",      description="Остановить VM",                  auth_config=_w),
            HttpEndpoint(method="POST", path=f"{b}/vms/{{vmid}}/reboot",      service="proxmox.vm.reboot",    description="Перезагрузить VM",               auth_config=_w),
            HttpEndpoint(method="GET",  path=f"{b}/vms/{{vmid}}/status",      service="proxmox.vm.status",    description="Статус VM",                      auth_config=_r),
            HttpEndpoint(method="GET",  path=f"{b}/containers",               service="proxmox.ct.list",      description="Список LXC-контейнеров",          auth_config=_r),
            HttpEndpoint(method="POST", path=f"{b}/containers/{{ctid}}/start",service="proxmox.ct.start",     description="Запустить контейнер",            auth_config=_w),
            HttpEndpoint(method="POST", path=f"{b}/containers/{{ctid}}/stop", service="proxmox.ct.stop",      description="Остановить контейнер",           auth_config=_w),
            HttpEndpoint(method="GET",  path=f"{b}/storage",                  service="proxmox.storage.list", description="Список хранилищ",                auth_config=_r),
            HttpEndpoint(method="GET",  path=f"{b}/backups",                  service="proxmox.backup.list",  description="Список бэкапов",                  auth_config=_r),
        ]:
            self.register_http_endpoint(ep)
